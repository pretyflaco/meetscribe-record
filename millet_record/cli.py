"""CLI entry point for millet-record (formerly meetscribe-record).

Provides the `millet` console script with capture-only subcommands:
    record    — record dual-channel meeting audio
    devices   — list audio sources
    check     — verify prerequisites
    archive   — compress past WAV recordings to OGG/Opus

When the optional `millet-pipeline` package is installed, additional
subcommands (transcribe, run, label, sync, gui, ...) are discovered
through the `millet.subcommands` entry-point group and added
dynamically.  The legacy `meet.subcommands` group is also consulted
for one deprecation cycle (millet-pipeline 0.9.x).

This is the Click "plugin" pattern: every package wishing to extend the
`millet` command registers Click command objects in `pyproject.toml`:

    [project.entry-points."millet.subcommands"]
    transcribe = "millet.cli:transcribe_cmd"

A second entry point `meet` is also published that prints a deprecation
warning and forwards to the same group.  Removed in millet-record 0.6.0.
"""
from __future__ import annotations

import datetime
import signal
import sys
import time
from functools import partial
from pathlib import Path

import click

from .capture import DRAIN_SECONDS
from .utils import fmt_elapsed, fmt_size

# ─── Drain countdown + recording loop ────────────────────────────────────────


def _drain_countdown(session, seconds: int = DRAIN_SECONDS) -> None:
    """Keep recording for *seconds* more to let ffmpeg's delayed pipeline flush.

    During the countdown:
    - Additional Ctrl+C signals are ignored (SIGINT → SIG_IGN)
    - A single status line updates in-place each second showing remaining time,
      elapsed recording time, and file size
    After the countdown, default SIGINT handling is restored.
    """
    prev_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        for remaining in range(seconds, 0, -1):
            status = session.status()
            elapsed = fmt_elapsed(status.elapsed_seconds)
            size = fmt_size(status.file_size_bytes)
            click.echo(
                f"\r\033[K\033[1;33m⏳ Flushing audio buffer... {remaining}s\033[0m"
                f"  {elapsed}  {size}",
                nl=False,
            )
            time.sleep(1)
        status = session.status()
        elapsed = fmt_elapsed(status.elapsed_seconds)
        size = fmt_size(status.file_size_bytes)
        click.echo(f"\r\033[K\033[1;32m✔ Buffer flushed\033[0m  {elapsed}  {size}")
    finally:
        signal.signal(signal.SIGINT, prev_handler)


def _recording_loop(session) -> None:
    """Run the live recording status display loop.

    Shows an updating single-line status indicator. Replaces signal.pause()
    with an active monitoring loop. Re-raises KeyboardInterrupt so the
    caller can drain and stop the session.
    """
    last_restart_count = 0
    warned_failed = False
    warned_system_silent = False

    try:
        while True:
            status = session.status()
            elapsed = fmt_elapsed(status.elapsed_seconds)
            size = fmt_size(status.file_size_bytes)

            # System-channel silence: warn once per silence episode, on its
            # own line (does not interrupt recording), and re-arm on recovery
            # so a second drop warns again.
            if status.system_silent and not warned_system_silent:
                click.echo(
                    f"\r\033[K\033[1;33m⚠ System audio silent\033[0m  "
                    f"{elapsed}  — remote participants may not be recorded. "
                    f"Check your meeting app's output device."
                )
                warned_system_silent = True
            elif not status.system_silent and warned_system_silent:
                click.echo(
                    f"\r\033[K\033[1;32m✔ System audio restored\033[0m  {elapsed}"
                )
                warned_system_silent = False

            if status.failed and not warned_failed:
                reason = status.fail_reason or "unknown error"
                click.echo(
                    f"\r\033[K\033[1;31m✖ RECORDING FAILED\033[0m  "
                    f"{elapsed}  {size}  — {reason}"
                )
                click.echo("  Press Ctrl+C to keep what was captured.")
                warned_failed = True
            elif status.restart_count > last_restart_count:
                last_restart_count = status.restart_count
                click.echo(
                    f"\r\033[K\033[1;33m⚠ Recording restarted\033[0m "
                    f"(attempt {status.restart_count})  {elapsed}  {size}"
                )
            elif not warned_failed:
                if status.is_alive:
                    line = (
                        f"\r\033[K\033[1;32m● REC\033[0m  "
                        f"{elapsed}  {size}  Ctrl+C to stop"
                    )
                else:
                    line = (
                        f"\r\033[K\033[1;33m● REC (starting...)\033[0m  "
                        f"{elapsed}  {size}"
                    )
                click.echo(line, nl=False)

            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\r\033[K", nl=False)
        raise


# ─── Top-level group with entry-point plugin discovery ───────────────────────


def _load_plugin_subcommands(group: click.Group) -> None:
    """Discover and add subcommands registered under `millet.subcommands`.

    Other packages (e.g. `millet-pipeline`) extend the `millet` CLI by
    declaring `millet.subcommands` entry points pointing at Click
    command objects.  Failure to load any one plugin is logged but does
    not break the CLI.

    For one deprecation cycle, the legacy `meet.subcommands` group is
    also consulted — that's how millet-pipeline 0.9.x dual-publishes
    so users on the old `meet` console-script alias still get the full
    feature set.  Removed in millet-record 0.6.0.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover — Python < 3.10 not supported
        return

    eps_seen: set[str] = set()
    for group_name in ("millet.subcommands", "meet.subcommands"):
        try:
            eps = entry_points(group=group_name)
        except TypeError:
            # Older importlib_metadata API
            eps = entry_points().get(group_name, [])

        for ep in eps:
            if ep.name in eps_seen:
                continue
            eps_seen.add(ep.name)
            try:
                cmd = ep.load()
            except Exception as exc:
                # Don't let a broken plugin take down the whole CLI;
                # warn and skip.
                click.echo(
                    f"warning: failed to load `millet` plugin "
                    f"{ep.name!r}: {exc}",
                    err=True,
                )
                continue
            if isinstance(cmd, click.Command) and ep.name not in group.commands:
                group.add_command(cmd, name=ep.name)


from . import __version__


def _combined_version() -> str:
    """Format a `millet --version` string mentioning both packages when present.

    Resolution preference: ``millet-pipeline`` (current name) > legacy
    ``meetscribe-offline`` (pre-rename name).  When the legacy package
    is found, label the version string accordingly so users aren't
    misled into thinking they have the new package installed when they
    actually have the old one — the mislabel surfaced during the
    2026-05-24 laptop smoke when a user with only legacy
    ``meetscribe-offline 0.7.1`` installed saw "millet-pipeline 0.7.1"
    in their --version output and assumed they had the new package.

    Example outputs:
        millet 0.9.0 (millet-pipeline 0.9.0; millet-record 0.4.1)
        millet 0.7.1 (meetscribe-offline 0.7.1 [legacy — `pip install \
millet-pipeline` to upgrade]; millet-record 0.4.1)
        millet 0.4.1 (millet-record 0.4.1; pipeline not installed — \
`pip install millet-pipeline` for transcription/diarization)
    """
    record_v = __version__
    pipeline_v: str | None = None
    pipeline_label = "millet-pipeline"  # source identifier
    legacy_fallback = False
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            pipeline_v = version("millet-pipeline")
        except PackageNotFoundError:
            try:
                # Transitional: pre-rename name might still be installed.
                pipeline_v = version("meetscribe-offline")
                pipeline_label = "meetscribe-offline"
                legacy_fallback = True
            except PackageNotFoundError:
                pipeline_v = None
    except Exception:
        pipeline_v = None

    if pipeline_v is None:
        return (
            f"{record_v} (millet-record {record_v}; "
            f"pipeline not installed — "
            f"`pip install millet-pipeline` for transcription/diarization)"
        )
    if legacy_fallback:
        return (
            f"{pipeline_v} ({pipeline_label} {pipeline_v} "
            f"[legacy — `pip install millet-pipeline` to upgrade]; "
            f"millet-record {record_v})"
        )
    return (
        f"{pipeline_v} ({pipeline_label} {pipeline_v}; "
        f"millet-record {record_v})"
    )


@click.group()
@click.version_option(version=_combined_version(), prog_name="millet")
def main():
    """Meeting audio recorder.

    Capture-only subcommands ship in `millet-record`.  Install
    `millet-pipeline` to add transcription, diarization, labeling,
    summarization, sync, and GUI subcommands.
    """
    pass


def _deprecated_meet_main() -> None:
    """Deprecation shim for the legacy ``meet`` console script.

    Prints a one-time warning, then forwards to the ``millet`` group.
    Removed in millet-record 0.6.0.
    """
    import os
    import warnings
    if os.environ.get("MILLET_SUPPRESS_DEPRECATION") != "1":
        warnings.warn(
            "The `meet` command is deprecated and will be removed in "
            "millet-record 0.6.0.  Use `millet` instead.  Set "
            "MILLET_SUPPRESS_DEPRECATION=1 to silence this warning.",
            DeprecationWarning,
            stacklevel=2,
        )
        # warnings module silently drops DeprecationWarning by default; also
        # echo to stderr so the user actually sees it.
        click.echo(
            "warning: `meet` is deprecated; use `millet` instead.",
            err=True,
        )
    main()


# ─── record ──────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--output-dir", "-o", type=click.Path(), default=None,
    help="Directory to save recordings (default: ~/meet-recordings)",
)
@click.option(
    "--filename", "-f", type=str, default=None,
    help="Output filename (default: meeting-YYYYMMDD-HHMMSS.wav)",
)
@click.option(
    "--mic", type=str, default=None,
    help="Mic source name (default: system default)",
)
@click.option(
    "--monitor", type=str, default=None,
    help="Monitor source name (default: default sink monitor)",
)
@click.option(
    "--virtual-sink", is_flag=True, default=False,
    help="Use a virtual sink for isolated capture",
)
@click.option(
    "--capture-backend",
    type=click.Choice(["default", "pocketstation"]),
    default="default",
    show_default=True,
    help="Recorder to use",
)
@click.option(
    "--application", type=str, default=None,
    help="Desktop application name or process ID for PocketStation capture",
)
def record(output_dir, filename, mic, monitor, virtual_sink, capture_backend, application):
    """Record meeting audio. Press Ctrl+C to stop."""
    if capture_backend == "pocketstation":
        from .pocketstation_capture import (
            check_pocketstation_prerequisites,
            create_pocketstation_session,
        )

        if application is None:
            raise click.UsageError(
                "--application is required with --capture-backend pocketstation"
            )
        try:
            application_selector: str | int = int(application)
        except ValueError:
            application_selector = application
        issues = check_pocketstation_prerequisites()
        create_selected_session = partial(
            create_pocketstation_session,
            application=application_selector,
            output_dir=output_dir,
            filename=filename,
            microphone=mic or True,
        )
    else:
        from .capture import check_prerequisites, create_session

        if application is not None:
            raise click.UsageError(
                "--application requires --capture-backend pocketstation"
            )
        issues = check_prerequisites()
        create_selected_session = partial(
            create_session,
            output_dir=output_dir,
            filename=filename,
            mic=mic,
            monitor=monitor,
            virtual_sink=virtual_sink,
        )
    if issues:
        click.echo("Prerequisites check failed:", err=True)
        for issue in issues:
            click.echo(f"  - {issue}", err=True)
        sys.exit(1)
    session = create_selected_session()

    click.echo(f"Recording to: {session.output_file}")
    click.echo(f"  Mic source:     {session.mic_source}")
    click.echo(f"  Monitor source: {session.monitor_source}")
    click.echo(f"  Virtual sink:   {session.use_virtual_sink}")
    if virtual_sink:
        click.echo(
            "  NOTE: Route your meeting app's audio to 'Meet-Capture' in pavucontrol"
        )
    click.echo()

    session.start()

    # The recorder subprocess is start_new_session=True-detached, so it
    # never sees the terminal's signals itself — if this parent dies
    # without calling session.stop(), the recorder keeps recording until
    # the disk fills (ffmpeg does not exit on stdin EOF).  Two guards:
    #   1. SIGTERM (systemd stop, kill, session logout) is translated
    #      into the same KeyboardInterrupt path as Ctrl+C, giving a
    #      graceful drain + stop.
    #   2. A finally-block stops the session on ANY other exit path
    #      (unexpected exception from the status loop, etc.).
    def _sigterm_to_interrupt(signum, frame):
        raise KeyboardInterrupt

    prev_sigterm = signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    stopped = False
    try:
        _recording_loop(session)
    except KeyboardInterrupt:
        if getattr(session, "needs_drain", True):
            _drain_countdown(session)
        click.echo("Stopping recording...")
        output = session.stop()
        stopped = True
        if output.exists():
            size_mb = output.stat().st_size / (1024 * 1024)
            click.echo(f"Saved: {output} ({size_mb:.1f} MB)")
            status = session.status()
            if status.restart_count > 0:
                click.echo(
                    f"  Note: recording restarted {status.restart_count} time(s) "
                    f"— check .ffmpeg.log if audio seems off"
                )
        else:
            click.echo("Warning: output file was not created", err=True)
        sys.exit(0)
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        if not stopped:
            # Reached on an unexpected exception (or SystemExit raised
            # before stop completed): make sure no detached recorder
            # outlives us.
            try:
                session.stop()
            except Exception:
                pass


# ─── devices ─────────────────────────────────────────────────────────────────


@main.command()
def devices():
    """List available audio devices."""
    from .capture import get_default_sink, get_default_source, list_sources

    try:
        default_source = get_default_source()
        default_sink = get_default_sink()
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"Default mic (source):  {default_source}")
    click.echo(f"Default output (sink): {default_sink}")
    click.echo(f"Monitor source:        {default_sink}.monitor")
    click.echo()

    sources = list_sources()

    click.echo("All sources:")
    click.echo(f"  {'IDX':<5} {'STATE':<12} {'NAME'}")
    click.echo(f"  {'---':<5} {'-----':<12} {'----'}")
    for src in sources:
        marker = ""
        if src.name == default_source:
            marker = " <-- default mic"
        elif src.is_monitor and src.name == f"{default_sink}.monitor":
            marker = " <-- default monitor"
        click.echo(f"  {src.index:<5} {src.state:<12} {src.name}{marker}")


# ─── check ───────────────────────────────────────────────────────────────────


@main.command()
def check():
    """Check system prerequisites for recording.

    Verifies ffmpeg + PulseAudio/PipeWire. If meetscribe-offline is also
    installed, additionally probes whisperx, CUDA, and HF_TOKEN.
    """
    import os

    from .capture import check_prerequisites

    click.echo("Checking prerequisites...")
    click.echo()

    issues = check_prerequisites()
    if issues:
        click.echo("Issues found:")
        for issue in issues:
            click.echo(f"  - {issue}")
        sys.exit(1)
    else:
        click.echo("  ffmpeg:              OK")
        if sys.platform == "darwin":
            click.echo("  mic permission:      OK")
            click.echo("  system audio perm:   OK")
        else:
            click.echo("  PulseAudio/PipeWire: OK")

    # Probe optional offline-only deps without requiring them.
    click.echo()
    try:
        import whisperx  # noqa: F401
        click.echo("  whisperx:         OK")
        offline_present = True
    except ImportError:
        click.echo("  whisperx:         NOT INSTALLED  "
                   "(only needed if you also want transcription; "
                   "pip install meetscribe-offline)")
        offline_present = False

    if offline_present:
        try:
            import torch
            cuda_available = torch.cuda.is_available()
            if cuda_available:
                gpu_name = torch.cuda.get_device_name(0)
                click.echo(f"  CUDA:             OK ({gpu_name})")
            else:
                click.echo("  CUDA:             Not available (will use CPU)")
        except ImportError:
            click.echo("  torch:            NOT INSTALLED")

        hf_token = os.environ.get("HF_TOKEN")
        if not hf_token:
            token_path = Path.home() / ".cache" / "huggingface" / "token"
            if token_path.exists():
                hf_token = token_path.read_text().strip()
        if hf_token:
            click.echo("  HF_TOKEN:         OK")
        else:
            click.echo("  HF_TOKEN:         NOT SET (diarization won't work)")
            click.echo("                    Set with: export HF_TOKEN=hf_...")

    click.echo()
    click.echo("All recording prerequisites met.")


# ─── archive ─────────────────────────────────────────────────────────────────


@main.command()
@click.argument("session_dirs", nargs=-1, type=click.Path(exists=True))
@click.option(
    "--older-than", type=int, default=None,
    help="Only compress sessions older than N days",
)
@click.option(
    "--keep-wav", is_flag=True, default=False,
    help="Keep original WAV files after compression",
)
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Show what would be compressed without doing it",
)
def archive(session_dirs, older_than, keep_wav, dry_run):
    """Compress session WAV files to OGG/Opus to save disk space.

    If no SESSION_DIRS are given, scans ~/meet-recordings/ for all sessions
    that still have uncompressed WAV files.

    \b
    Examples:
        meet archive
        meet archive --dry-run
        meet archive --older-than 7
        meet archive ~/meet-recordings/meeting-20260325-150203_LABEL
        meet archive --keep-wav ~/meet-recordings/meeting-*
    """
    from .audio import compress_audio

    recordings_dir = Path.home() / "meet-recordings"

    if session_dirs:
        dirs = [Path(d) for d in session_dirs]
    elif recordings_dir.is_dir():
        dirs = sorted([d for d in recordings_dir.iterdir() if d.is_dir()])
    else:
        click.echo(f"No session directories found in {recordings_dir}")
        return

    targets: list[Path] = []
    for d in dirs:
        for wav in sorted(d.glob("*.wav")):
            if ".chunk-" in wav.name:
                continue
            if wav.with_suffix(".ogg").exists():
                continue
            if older_than is not None:
                mtime = datetime.datetime.fromtimestamp(wav.stat().st_mtime)
                age_days = (datetime.datetime.now() - mtime).days
                if age_days < older_than:
                    continue
            targets.append(wav)

    if not targets:
        click.echo("No uncompressed WAV files to archive.")
        return

    total_wav_size = sum(w.stat().st_size for w in targets)
    click.echo(
        f"Found {len(targets)} WAV file(s) totaling "
        f"{total_wav_size / 1_048_576:.1f} MB"
    )

    if dry_run:
        click.echo()
        for wav in targets:
            size_mb = wav.stat().st_size / 1_048_576
            click.echo(
                f"  [DRY RUN] {wav.parent.name}/{wav.name} ({size_mb:.1f} MB)"
            )
        estimated_ratio = 10.5
        estimated_ogg = total_wav_size / estimated_ratio
        click.echo(
            f"\n  Estimated compressed size: ~{estimated_ogg / 1_048_576:.0f} MB "
            f"(~{estimated_ratio:.0f}x reduction)"
        )
        return

    compressed_count = 0
    saved_bytes = 0
    for wav in targets:
        label = f"{wav.parent.name}/{wav.name}"
        wav_size = wav.stat().st_size
        click.echo(
            f"  Compressing {label} ({wav_size / 1_048_576:.1f} MB)...",
            nl=False,
        )
        try:
            ogg_path = compress_audio(wav, keep_wav=keep_wav)
            ogg_size = ogg_path.stat().st_size
            ratio = wav_size / ogg_size if ogg_size > 0 else 0
            saved = wav_size - ogg_size
            saved_bytes += saved
            compressed_count += 1
            click.echo(f" -> {ogg_size / 1_048_576:.1f} MB ({ratio:.0f}x)")
        except Exception as exc:
            click.echo(f" FAILED: {exc}", err=True)

    click.echo(
        f"\nDone: {compressed_count}/{len(targets)} files compressed, "
        f"{saved_bytes / 1_048_576:.0f} MB saved"
    )


# Hook plugin discovery onto the group AFTER all built-in commands are
# registered, so plugins can override (the entry-points loader skips names
# already present, preserving built-in semantics).
_load_plugin_subcommands(main)


if __name__ == "__main__":
    main()
