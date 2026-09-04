"""Optional PocketStation capture for one desktop application and microphone."""

from __future__ import annotations

import importlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from .capture import RecordingStatus


def _load_pocketstation() -> ModuleType:
    try:
        return importlib.import_module("pocketstation")
    except ImportError as error:
        raise RuntimeError(
            "PocketStation capture is not installed. "
            "Run: pip install 'millet-record[pocketstation]'"
        ) from error


def check_pocketstation_prerequisites() -> list[str]:
    """Return anything that prevents PocketStation capture from starting."""
    issues: list[str] = []
    try:
        _load_pocketstation()
    except RuntimeError as error:
        issues.append(str(error))

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=10)
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        issues.append("ffmpeg is required to create Millet's stereo WAV")
    return issues


@dataclass
class PocketStationRecordingSession:
    """Record one application and microphone through PocketStation."""

    output_dir: Path
    output_file: Path
    application: str | int
    microphone: bool | str = True
    mic_source: str = field(init=False)
    monitor_source: str = field(init=False)
    use_virtual_sink: bool = field(default=False, init=False)
    needs_drain: bool = field(default=False, init=False)

    _capture: Any | None = field(default=None, init=False, repr=False)
    _current_root: Path | None = field(default=None, init=False, repr=False)
    _recordings: list[Path] = field(default_factory=list, init=False, repr=False)
    _started_at: float = field(default=0.0, init=False, repr=False)
    _recorded_seconds: float = field(default=0.0, init=False, repr=False)
    _paused: bool = field(default=False, init=False, repr=False)
    _failed: bool = field(default=False, init=False, repr=False)
    _fail_reason: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        self.output_file = Path(self.output_file)
        self.monitor_source = str(self.application)
        self.mic_source = "default" if self.microphone is True else str(self.microphone)

    def start(self) -> None:
        """Start a new PocketStation recording."""
        if self._capture is not None:
            raise RuntimeError("Recording has already started")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._start_capture()

    def stop(self) -> Path:
        """Stop capture and create Millet's left-mic, right-app stereo WAV."""
        if self._capture is not None:
            self._finish_capture()
        if not self._recordings:
            raise RuntimeError("PocketStation did not produce a recording")

        stereo_chunks: list[Path] = []
        for index, recording in enumerate(self._recordings):
            chunk = self.output_dir / f"{self.output_file.stem}.pks-{index:03d}.wav"
            self._make_stereo(recording, chunk)
            stereo_chunks.append(chunk)

        if len(stereo_chunks) == 1:
            stereo_chunks[0].replace(self.output_file)
        else:
            self._concat(stereo_chunks)

        for chunk in stereo_chunks:
            chunk.unlink(missing_ok=True)
        self._write_metadata()
        return self.output_file

    def pause(self) -> None:
        """Finish the current recording until :meth:`resume` is called."""
        if self._capture is None or self._paused:
            raise RuntimeError("Recording is not active")
        self._finish_capture()
        self._paused = True

    def resume(self) -> None:
        """Resume into another recording that is joined during stop."""
        if not self._paused:
            raise RuntimeError("Recording is not paused")
        self._paused = False
        self._start_capture()

    def status(self) -> RecordingStatus:
        """Return progress using PocketStation's recorded files."""
        elapsed = self._recorded_seconds
        if self._capture is not None:
            elapsed += time.monotonic() - self._started_at
        recording_roots = list(self._recordings)
        if self._current_root is not None:
            recording_roots.append(self._current_root)
        file_size = sum(
            path.stat().st_size
            for recording in recording_roots
            for path in recording.glob("**/stems/*.wav")
            if path.exists()
        )
        return RecordingStatus(
            is_alive=self._capture is not None and self._capture.is_running,
            elapsed_seconds=elapsed,
            file_size_bytes=file_size,
            restart_count=0,
            failed=self._failed,
            fail_reason=self._fail_reason,
            paused=self._paused,
            system_silent=False,
            system_ever_active=False,
        )

    def _start_capture(self) -> None:
        pks = _load_pocketstation()
        root = self.output_dir / (f".{self.output_file.stem}-pocketstation-{len(self._recordings)}")
        capture = pks.capture(
            application=self.application,
            microphone=self.microphone,
            record_to=root,
            stream_audio=False,
        )
        try:
            capture.start()
        except Exception as error:
            capture.close()
            self._failed = True
            self._fail_reason = str(error)
            raise
        self._capture = capture
        self._current_root = root
        self._started_at = time.monotonic()

    def _finish_capture(self) -> None:
        capture = self._capture
        if capture is None:
            return
        self._capture = None
        self._recorded_seconds += time.monotonic() - self._started_at
        try:
            result = capture.stop()
        finally:
            capture.close()
        outcome = result.recording
        if outcome is None or not outcome.complete:
            self._failed = True
            self._fail_reason = "PocketStation could not finish the recording"
            raise RuntimeError(self._fail_reason)
        self._recordings.append(outcome.session_directory)
        self._current_root = None

    def _make_stereo(self, recording: Path, output: Path) -> None:
        microphone = recording / "stems" / "microphone.wav"
        application = recording / "stems" / "application.wav"
        if not microphone.is_file() or not application.is_file():
            raise RuntimeError(
                "PocketStation recording is missing an application or microphone stem"
            )
        command = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(microphone),
            "-i",
            str(application),
            "-filter_complex",
            "[0:a]aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono[mic];"
            "[1:a]aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono[app];"
            "[mic][app]join=inputs=2:channel_layout=stereo[out]",
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("ffmpeg timed out while creating the stereo recording") from error
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg could not create the stereo recording: {result.stderr.strip()}"
            )

    def _concat(self, chunks: list[Path]) -> None:
        concat_file = self.output_dir / f".{self.output_file.stem}-pks-concat.txt"
        entries: list[str] = []
        for path in chunks:
            safe_path = path.as_posix().replace("'", "'\\''")
            entries.append(f"file '{safe_path}'\n")
        concat_file.write_text("".join(entries))
        try:
            try:
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(concat_file),
                        "-c",
                        "copy",
                        str(self.output_file),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("ffmpeg timed out while joining recording segments") from error
        finally:
            concat_file.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg could not join recording segments: {result.stderr.strip()}")

    def _write_metadata(self) -> None:
        metadata = {
            "capture_backend": "pocketstation",
            "application": self.application,
            "microphone": self.mic_source,
            "output_file": str(self.output_file),
            "file_exists": self.output_file.exists(),
            "file_size_bytes": self.output_file.stat().st_size,
            "recording_segments": len(self._recordings),
        }
        self.output_file.with_suffix(".session.json").write_text(json.dumps(metadata, indent=2))


def create_pocketstation_session(
    *,
    application: str | int,
    output_dir: str | Path | None = None,
    filename: str | None = None,
    microphone: bool | str = True,
) -> PocketStationRecordingSession:
    """Create the optional PocketStation recording session."""
    if output_dir is None:
        output_dir = Path.home() / "meet-recordings"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        session_name = f"meeting-{timestamp}"
        output_dir = output_dir / session_name
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{session_name}.wav"
    return PocketStationRecordingSession(
        output_dir=output_dir,
        output_file=output_dir / filename,
        application=application,
        microphone=microphone,
    )


__all__ = [
    "PocketStationRecordingSession",
    "check_pocketstation_prerequisites",
    "create_pocketstation_session",
]
