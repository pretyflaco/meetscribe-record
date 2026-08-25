"""Tests for per-channel activity metering and system-silence detection.

Covers:
* ``audio.sample_channel_rms`` — tail-sampling per-channel active RMS from a
  real stereo WAV (via ffmpeg), plus its conservative ``None`` returns.
* ``RecordingSession._check_system_channel`` — the watchdog logic that flags a
  system (remote) channel going silent mid-recording, and the "ever active"
  gate that keeps genuine in-room meetings from warning.
* Propagation of the flag through ``RecordingStatus`` and into metadata.

Real ``ffmpeg`` IS invoked by the ``sample_channel_rms`` tests (they write and
read a genuine WAV); they skip if ffmpeg is unavailable.  The watchdog-logic
tests mock ``sample_channel_rms`` so they need no audio tooling.
"""
from __future__ import annotations

import math
import shutil
import struct
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

import millet_record.audio as audio
import millet_record.capture as capture

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_needs_ffmpeg = pytest.mark.skipif(not _HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


def _write_stereo_wav(
    path: Path,
    mic_amp: int,
    sys_amp: int,
    *,
    seconds: float = 4.0,
    rate: int = 16000,
    freq: float = 220.0,
) -> None:
    """Write a stereo s16le WAV: L=mic tone at mic_amp, R=system tone at sys_amp."""
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        t = i / rate
        s = math.sin(2 * math.pi * freq * t)
        left = int(mic_amp * s)
        right = int(sys_amp * s)
        frames += struct.pack("<hh", left, right)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


# ── sample_channel_rms ───────────────────────────────────────────────────────


@_needs_ffmpeg
def test_sample_channel_rms_both_active(tmp_path):
    wav = tmp_path / "both.wav"
    _write_stereo_wav(wav, mic_amp=8000, sys_amp=8000)
    rms = audio.sample_channel_rms(wav)
    assert rms is not None
    mic_rms, sys_rms = rms
    assert mic_rms > 1000
    assert sys_rms > 1000
    # Equal amplitudes → comparable RMS.
    assert abs(mic_rms - sys_rms) < 0.2 * mic_rms


@_needs_ffmpeg
def test_sample_channel_rms_system_silent(tmp_path):
    wav = tmp_path / "silent_sys.wav"
    _write_stereo_wav(wav, mic_amp=8000, sys_amp=0)
    rms = audio.sample_channel_rms(wav)
    assert rms is not None
    mic_rms, sys_rms = rms
    assert mic_rms > 1000
    # System channel is all zeros → below the 10% ratio.
    assert sys_rms <= capture._SYSTEM_SILENT_RATIO * mic_rms


@_needs_ffmpeg
def test_sample_channel_rms_mono_returns_none(tmp_path):
    wav = tmp_path / "mono.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 1000) * 16000)
    assert audio.sample_channel_rms(wav) is None


def test_sample_channel_rms_missing_file_returns_none(tmp_path):
    assert audio.sample_channel_rms(tmp_path / "nope.wav") is None


# ── _check_system_channel watchdog logic ─────────────────────────────────────


def _session(tmp_path) -> capture.RecordingSession:
    return capture.RecordingSession(
        output_dir=tmp_path,
        output_file=tmp_path / "out.wav",
        mic_source="mic",
        monitor_source="sink.monitor",
    )


def test_system_active_sets_ever_active(tmp_path):
    sess = _session(tmp_path)
    chunk = tmp_path / "c.wav"
    chunk.write_bytes(b"x")  # existence only; sampling is mocked
    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 4000.0)):
        sess._check_system_channel(chunk)
    assert sess._system_ever_active is True
    assert sess._system_silent is False


def test_silent_before_ever_active_does_not_flag(tmp_path):
    """In-room meeting: system channel never had audio → never warns."""
    sess = _session(tmp_path)
    chunk = tmp_path / "c.wav"
    chunk.write_bytes(b"x")
    # Mic active, system silent, but system was never active before.
    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 0.0)):
        for _ in range(10):
            sess._check_system_channel(chunk)
    assert sess._system_ever_active is False
    assert sess._system_silent is False


def test_silence_flags_after_timeout(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    chunk = tmp_path / "c.wav"
    chunk.write_bytes(b"x")

    # 1) System active first → arms the "ever active" gate at t=0.
    fake_now = [1000.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: fake_now[0])
    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 4000.0)):
        sess._check_system_channel(chunk)
    assert sess._system_ever_active is True

    # 2) System goes silent while mic stays active — first silent sample only
    #    *arms* the timer; not yet flagged.
    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 0.0)):
        fake_now[0] = 1000.0
        sess._check_system_channel(chunk)
        assert sess._system_silent is False

        # A later sample still within the window → still not flagged.
        fake_now[0] = 1000.0 + capture._SYSTEM_SILENCE_TIMEOUT - 1
        sess._check_system_channel(chunk)
        assert sess._system_silent is False

        # 3) A sample past the timeout since arming → flagged.
        fake_now[0] = 1000.0 + capture._SYSTEM_SILENCE_TIMEOUT + 1
        sess._check_system_channel(chunk)
    assert sess._system_silent is True
    assert sess._system_silent_detected is True


def test_silence_timeout_is_42s():
    assert capture._SYSTEM_SILENCE_TIMEOUT == 42.0


def test_lull_does_not_flag(tmp_path, monkeypatch):
    """Nobody talking on either channel is a lull, not a silent system."""
    sess = _session(tmp_path)
    chunk = tmp_path / "c.wav"
    chunk.write_bytes(b"x")
    fake_now = [0.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: fake_now[0])

    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 4000.0)):
        sess._check_system_channel(chunk)  # arm ever-active

    # Both channels silent for a long time.
    with patch.object(capture, "sample_channel_rms", return_value=(0.0, 0.0)):
        fake_now[0] = 10_000.0
        sess._check_system_channel(chunk)
    assert sess._system_silent is False


def test_recovery_clears_flag(tmp_path, monkeypatch):
    sess = _session(tmp_path)
    chunk = tmp_path / "c.wav"
    chunk.write_bytes(b"x")
    fake_now = [0.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: fake_now[0])

    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 4000.0)):
        sess._check_system_channel(chunk)
    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 0.0)):
        fake_now[0] = 100.0  # arm the silence timer
        sess._check_system_channel(chunk)
        fake_now[0] = 100.0 + capture._SYSTEM_SILENCE_TIMEOUT + 1  # past timeout
        sess._check_system_channel(chunk)
    assert sess._system_silent is True

    # System audio returns.
    with patch.object(capture, "sample_channel_rms", return_value=(5000.0, 4000.0)):
        sess._check_system_channel(chunk)
    assert sess._system_silent is False
    # Historical detection flag stays set for metadata.
    assert sess._system_silent_detected is True


def test_unknown_sample_is_not_silent(tmp_path):
    sess = _session(tmp_path)
    sess._system_ever_active = True
    chunk = tmp_path / "c.wav"
    chunk.write_bytes(b"x")
    with patch.object(capture, "sample_channel_rms", return_value=None):
        sess._check_system_channel(chunk)
    assert sess._system_silent is False


# ── status + metadata propagation ────────────────────────────────────────────


def test_status_exposes_system_silent(tmp_path):
    sess = _session(tmp_path)
    sess._system_silent = True
    sess._system_ever_active = True
    st = sess.status()
    assert st.system_silent is True
    assert st.system_ever_active is True
