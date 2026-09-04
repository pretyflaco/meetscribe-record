"""PocketStation capture stays optional and preserves Millet's WAV layout."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from click.testing import CliRunner

from millet_record import cli
from millet_record import pocketstation_capture as capture


class _FakeCapture:
    def __init__(self, recording_root: Path) -> None:
        self.is_running = False
        self.closed = False
        self.recording = recording_root / "session-1"
        stems = self.recording / "stems"
        stems.mkdir(parents=True)
        (stems / "application.wav").write_bytes(b"application")
        (stems / "microphone.wav").write_bytes(b"microphone")

    def start(self) -> None:
        self.is_running = True

    def stop(self):
        self.is_running = False
        outcome = SimpleNamespace(complete=True, session_directory=self.recording)
        return SimpleNamespace(recording=outcome)

    def close(self) -> None:
        self.closed = True


def _install_fake_pocketstation(monkeypatch, calls: list[dict]) -> None:
    module = ModuleType("pocketstation")

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return _FakeCapture(Path(kwargs["record_to"]))

    module.capture = fake_capture
    monkeypatch.setitem(sys.modules, "pocketstation", module)


def test_session_records_app_and_mic_then_creates_stereo_wav(monkeypatch, tmp_path):
    calls: list[dict] = []
    _install_fake_pocketstation(monkeypatch, calls)

    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"stereo-wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    session = capture.create_pocketstation_session(
        application="Zoom",
        output_dir=tmp_path,
        filename="meeting.wav",
    )

    session.start()
    assert session.status().is_alive
    output = session.stop()

    assert calls == [
        {
            "application": "Zoom",
            "microphone": True,
            "record_to": tmp_path / ".meeting-pocketstation-0",
            "stream_audio": False,
        }
    ]
    assert output.read_bytes() == b"stereo-wav"
    assert output.with_suffix(".session.json").is_file()


def test_cli_requires_application_for_pocketstation():
    result = CliRunner().invoke(
        cli.main,
        ["record", "--capture-backend", "pocketstation"],
    )
    assert result.exit_code == 2
    assert "--application is required" in result.output


def test_cli_does_not_accept_application_with_default_recorder():
    result = CliRunner().invoke(cli.main, ["record", "--application", "Zoom"])
    assert result.exit_code == 2
    assert "--application requires --capture-backend pocketstation" in result.output
