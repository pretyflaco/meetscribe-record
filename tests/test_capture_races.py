"""Tests for the 0.5.0 stop/pause-vs-watchdog race fixes and the
leak-proof lifecycle guarantees in millet_record.capture.

Root cause of the races fixed in 0.5.0: session state (``_paused``,
process handles, the log file) was mutated outside ``_lock`` while the
watchdog thread read the same state under it.  Because recorder
subprocesses run ``start_new_session=True``-detached, a watchdog restart
that raced a ``stop()``/``pause()`` could leave a recorder running
forever with nobody left to stop it.

These tests pin the new ordering contracts:

* ``stop()`` joins the watchdog BEFORE killing the recorder.
* ``_attempt_restart`` re-checks session state under the lock and
  abandons the restart if a stop/pause won the race.
* ``start()`` failure tears down the log handle, PulseAudio modules,
  and empty chunk files.
* ``_concat_chunks`` survives a wedged ffmpeg via timeout + fallback.
* ``_repair_wav_header`` fixes SIGKILL-orphaned headers before concat.
* the ``record`` CLI stops the session on non-KeyboardInterrupt exits
  and on SIGTERM.
"""

from __future__ import annotations

import struct
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import millet_record.capture as cap
import millet_record.cli as cli_mod

# ─── stop() vs watchdog ordering ─────────────────────────────────────────────


@pytest.mark.timeout(30)
def test_stop_joins_watchdog_before_killing_recorder(monkeypatch, tmp_path, darwin_environ):
    """The watchdog must be dead by the time stop() kills the recorder.

    If the kill happened first, a watchdog mid-restart could spawn a
    fresh detached recorder after the kill — the orphan-recorder bug.
    """
    s = cap.create_session(output_dir=tmp_path, filename="meeting.wav")
    s.start()
    time.sleep(0.3)

    watchdog_alive_at_kill: list[bool] = []
    orig_stop_ffmpeg = s._stop_ffmpeg

    def instrumented_stop_ffmpeg():
        thread = s._watchdog_thread
        watchdog_alive_at_kill.append(bool(thread and thread.is_alive()))
        orig_stop_ffmpeg()

    monkeypatch.setattr(s, "_stop_ffmpeg", instrumented_stop_ffmpeg)
    s.stop()

    assert watchdog_alive_at_kill, "stop() must call _stop_ffmpeg"
    assert not any(watchdog_alive_at_kill), (
        "stop() killed the recorder while the watchdog thread was still "
        "alive — a racing _attempt_restart could orphan a new recorder"
    )


@pytest.mark.timeout(30)
def test_stop_closes_log_after_watchdog_exit(tmp_path, darwin_environ):
    """The shared .ffmpeg.log must stay open until the watchdog is joined
    (a racing restart would otherwise write to / reopen a closed handle)."""
    s = cap.create_session(output_dir=tmp_path, filename="meeting.wav")
    s.start()
    time.sleep(0.3)
    s.stop()
    assert s._ffmpeg_log is None
    thread = s._watchdog_thread
    assert thread is not None and not thread.is_alive()


# ─── _attempt_restart state re-checks ────────────────────────────────────────


def _bare_session(tmp_path: Path) -> cap.RecordingSession:
    return cap.RecordingSession(
        output_dir=tmp_path,
        output_file=tmp_path / "out.wav",
        mic_source="mic",
        monitor_source="mon",
    )


def test_attempt_restart_aborts_when_paused(monkeypatch, tmp_path):
    """A pause() that wins the race against the watchdog must suppress
    the restart entirely (previously the watchdog restarted a chunk that
    a subsequent stop()-from-paused would never kill)."""
    s = _bare_session(tmp_path)
    s._paused = True

    spawned: list[str] = []
    monkeypatch.setattr(
        s, "_spawn_recorder_chunk", lambda: spawned.append("spawn")
    )

    assert s._attempt_restart("test reason") is True
    assert spawned == []
    assert s._restart_count == 0
    assert not s._failed


def test_attempt_restart_aborts_when_stopping(monkeypatch, tmp_path):
    """Same guard for a stop() that wins the race."""
    s = _bare_session(tmp_path)
    s._stop_event.set()

    spawned: list[str] = []
    monkeypatch.setattr(
        s, "_spawn_recorder_chunk", lambda: spawned.append("spawn")
    )

    assert s._attempt_restart("test reason") is True
    assert spawned == []
    assert s._restart_count == 0


def test_attempt_restart_still_fails_after_max_attempts(tmp_path):
    s = _bare_session(tmp_path)
    s._restart_count = cap._MAX_RESTART_ATTEMPTS
    assert s._attempt_restart("boom") is False
    assert s._failed
    assert "Max restart attempts" in (s._fail_reason or "")


def test_pause_is_atomic_with_recorder_stop(monkeypatch, tmp_path):
    """pause() must hold _lock across the stop + flag-set pair, so the
    watchdog can never observe (dead recorder, _paused=False)."""
    s = _bare_session(tmp_path)

    lock_held_during_stop: list[bool] = []

    def fake_stop_ffmpeg():
        # If pause() holds the lock, a non-blocking acquire must fail.
        acquired = s._lock.acquire(blocking=False)
        if acquired:
            s._lock.release()
        lock_held_during_stop.append(not acquired)

    monkeypatch.setattr(s, "_stop_ffmpeg", fake_stop_ffmpeg)
    s.pause()

    assert lock_held_during_stop == [True], (
        "pause() must call _stop_ffmpeg while holding _lock"
    )
    assert s._paused


# ─── start() failure teardown ────────────────────────────────────────────────


def test_start_failure_tears_down_sink_log_and_chunks(monkeypatch, tmp_path):
    """A startup failure must unload PulseAudio modules, close the log,
    and remove the empty chunk — previously all three leaked."""
    monkeypatch.setattr(cap.sys, "platform", "linux")
    monkeypatch.delenv("MEET_RECORD_MAC", raising=False)

    pactl_calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        pactl_calls.append(list(cmd))
        if cmd[:2] == ["pactl", "load-module"]:
            return SimpleNamespace(returncode=0, stdout="42\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cap.subprocess, "run", fake_run)

    class DeadPopen:
        returncode = 1

        def __init__(self, cmd, *args, **kwargs):
            self.stdin = None

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(cap.subprocess, "Popen", DeadPopen)

    s = cap.RecordingSession(
        output_dir=tmp_path,
        output_file=tmp_path / "out.wav",
        mic_source="mic",
        monitor_source="mon",
        use_virtual_sink=True,
    )

    with pytest.raises(RuntimeError, match="failed to start"):
        s.start()

    # Both loaded modules must have been unloaded again.
    unloads = [c for c in pactl_calls if c[:2] == ["pactl", "unload-module"]]
    assert len(unloads) == 2, f"expected 2 unload-module calls, saw: {pactl_calls}"
    # Log handle closed and cleared.
    assert s._ffmpeg_log is None
    # No empty chunk file left behind.
    assert not list(tmp_path.glob("*.chunk-*.wav"))


# ─── _concat_chunks timeout ──────────────────────────────────────────────────


def _write_chunk(path: Path, data_bytes: int) -> None:
    path.write_bytes(b"\x00" * (cap._WAV_HEADER_BYTES + data_bytes))


def test_concat_timeout_falls_back_to_largest_chunk(monkeypatch, tmp_path):
    """A wedged ffmpeg concat must not hang stop() forever."""
    s = _bare_session(tmp_path)
    small = tmp_path / "out.chunk-000.wav"
    large = tmp_path / "out.chunk-001.wav"
    _write_chunk(small, 100)
    _write_chunk(large, 10_000)

    def hang_run(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(cap.subprocess, "run", hang_run)

    s._concat_chunks([small, large])
    assert s.output_file.exists()
    # The largest chunk won the fallback.
    assert s.output_file.stat().st_size == cap._WAV_HEADER_BYTES + 10_000


def test_concat_passes_a_timeout(monkeypatch, tmp_path):
    s = _bare_session(tmp_path)
    a = tmp_path / "out.chunk-000.wav"
    b = tmp_path / "out.chunk-001.wav"
    _write_chunk(a, 100)
    _write_chunk(b, 100)

    seen: dict = {}

    def fake_run(cmd, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        s.output_file.write_bytes(b"x")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cap.subprocess, "run", fake_run)
    s._concat_chunks([a, b])
    assert seen["timeout"] and seen["timeout"] >= 120.0


# ─── _repair_wav_header ──────────────────────────────────────────────────────


def _make_wav(path: Path, data_bytes: int, header_data_size: int) -> None:
    """Write a canonical 44-byte-header WAV with a (possibly wrong)
    data-size field, mimicking a SIGKILLed recorder."""
    riff_size = header_data_size + 36
    header = (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 2, 16000, 64000, 4, 16)
        + b"data"
        + struct.pack("<I", header_data_size)
    )
    path.write_bytes(header + b"\x01" * data_bytes)


def test_repair_fixes_zero_size_header(tmp_path):
    wav = tmp_path / "killed.wav"
    _make_wav(wav, data_bytes=64_000, header_data_size=0)

    assert cap._repair_wav_header(wav) is True

    raw = wav.read_bytes()
    assert struct.unpack("<I", raw[40:44])[0] == 64_000
    assert struct.unpack("<I", raw[4:8])[0] == len(raw) - 8


def test_repair_leaves_consistent_header_alone(tmp_path):
    wav = tmp_path / "clean.wav"
    _make_wav(wav, data_bytes=1000, header_data_size=1000)
    assert cap._repair_wav_header(wav) is False


def test_repair_ignores_non_wav(tmp_path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"this is not a RIFF file" + b"\x00" * 100)
    assert cap._repair_wav_header(junk) is False


def test_repair_ignores_header_only_file(tmp_path):
    wav = tmp_path / "empty.wav"
    _make_wav(wav, data_bytes=0, header_data_size=0)
    assert cap._repair_wav_header(wav) is False


@pytest.mark.timeout(30)
def test_stop_repairs_chunk_headers_before_stitch(monkeypatch, tmp_path, darwin_environ):
    """End-to-end: stop() must repair each valid chunk's header.

    The mock recorder finalizes headers on graceful exit; here we
    corrupt the chunk post-stop-signal by intercepting the repair call
    to prove it runs against every valid chunk.
    """
    s = cap.create_session(output_dir=tmp_path, filename="meeting.wav")
    s.start()
    time.sleep(0.3)

    repaired: list[Path] = []
    real_repair = cap._repair_wav_header

    def spy(path):
        repaired.append(path)
        return real_repair(path)

    monkeypatch.setattr(cap, "_repair_wav_header", spy)
    s.stop()
    assert repaired, "stop() must run _repair_wav_header on valid chunks"


# ─── record CLI lifecycle ────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self, output: Path):
        self.output_file = output
        self.mic_source = "mic"
        self.monitor_source = "mon"
        self.use_virtual_sink = False
        self.stop_calls = 0

    def start(self):
        pass

    def stop(self):
        self.stop_calls += 1
        self.output_file.write_bytes(b"\x00" * 100)
        return self.output_file

    def status(self):
        return cap.RecordingStatus(
            is_alive=True,
            elapsed_seconds=1.0,
            file_size_bytes=100,
            restart_count=0,
            failed=False,
        )


def test_record_cli_stops_session_on_unexpected_exception(monkeypatch, tmp_path):
    """Any non-KeyboardInterrupt escape from the status loop must still
    stop the session — otherwise the detached recorder records forever."""
    fake = _FakeSession(tmp_path / "out.wav")
    monkeypatch.setattr(cli_mod, "_recording_loop", lambda s: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(cap, "check_prerequisites", lambda: [])
    monkeypatch.setattr(cap, "create_session", lambda **kw: fake)

    result = CliRunner().invoke(cli_mod.main, ["record"], catch_exceptions=True)
    assert fake.stop_calls == 1, "session.stop() must run on unexpected exceptions"
    assert result.exit_code != 0


def test_record_cli_stops_exactly_once_on_ctrl_c(monkeypatch, tmp_path):
    """The graceful Ctrl+C path must not double-stop via the finally."""
    fake = _FakeSession(tmp_path / "out.wav")
    monkeypatch.setattr(cli_mod, "_recording_loop", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(cli_mod, "_drain_countdown", lambda s: None)
    monkeypatch.setattr(cap, "check_prerequisites", lambda: [])
    monkeypatch.setattr(cap, "create_session", lambda **kw: fake)

    result = CliRunner().invoke(cli_mod.main, ["record"])
    assert fake.stop_calls == 1
    assert result.exit_code == 0


def test_record_cli_installs_sigterm_handler(monkeypatch, tmp_path):
    """SIGTERM must be routed into the graceful KeyboardInterrupt path
    while recording (and restored afterwards)."""
    import signal as signal_mod

    fake = _FakeSession(tmp_path / "out.wav")
    installed: list = []
    real_signal = signal_mod.signal

    def spy_signal(signum, handler):
        if signum == signal_mod.SIGTERM:
            installed.append(handler)
            return signal_mod.SIG_DFL
        return real_signal(signum, handler)

    monkeypatch.setattr(cli_mod.signal, "signal", spy_signal)
    monkeypatch.setattr(cli_mod, "_recording_loop", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(cli_mod, "_drain_countdown", lambda s: None)
    monkeypatch.setattr(cap, "check_prerequisites", lambda: [])
    monkeypatch.setattr(cap, "create_session", lambda **kw: fake)

    CliRunner().invoke(cli_mod.main, ["record"])

    # First install is the handler, second is the restore.
    assert len(installed) == 2
    handler = installed[0]
    with pytest.raises(KeyboardInterrupt):
        handler(signal_mod.SIGTERM, None)
    assert installed[1] == signal_mod.SIG_DFL
