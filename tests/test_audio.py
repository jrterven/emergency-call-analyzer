import json
import shutil
import subprocess
import wave

import numpy as np
import pytest

from backend.audio import decode_audio, prepare_recording, probe_audio, resample_audio


def write_wav(path, samples, rate):
    matrix = samples[:, None] if samples.ndim == 1 else samples
    pcm = (matrix * 32767).astype("<i2")
    with wave.open(str(path), "wb") as target:
        target.setnchannels(matrix.shape[1])
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm.tobytes())


@pytest.mark.parametrize("rate", [44_100, 48_000])
def test_decode_channel_isolation_and_silence(tmp_path, rate):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for audio integration tests")
    t = np.arange(rate * 2) / rate
    caller = 0.3 * np.sin(2 * np.pi * 440 * t)
    caller[:rate] = 0
    assistant = 0.4 * np.sin(2 * np.pi * 880 * t)
    path = tmp_path / "stereo.wav"
    write_wav(path, np.column_stack([caller, assistant]), rate)

    assert probe_audio(path) == {"duration_ms": 2000, "channels": 2, "sample_rate": rate}
    left, right = decode_audio(path, 0), decode_audio(path, 1)
    assert left.dtype == np.float32
    assert len(left) == len(right) == 32_000
    assert np.max(np.abs(left[:15_900])) < 0.0001
    assert np.std(right[:16_000]) > 0.25
    spectrum = np.abs(np.fft.rfft(left[16_100:]))
    frequency = np.fft.rfftfreq(len(left[16_100:]), 1 / 16000)[np.argmax(spectrum)]
    assert frequency == pytest.approx(440, abs=2)
    with pytest.raises(ValueError, match="canal"):
        decode_audio(path, 2)


@pytest.mark.parametrize("rate", [44_100, 48_000])
def test_resample_preserves_silent_timebase(rate):
    samples = np.zeros(rate * 2, dtype=np.float32)
    samples[rate:rate + 10] = 0.2
    result = resample_audio(samples, rate)
    assert len(result) == 32_000
    assert result.dtype == np.float32
    assert np.max(np.abs(result[:15_900])) == 0
    assert np.max(result[15_990:16_020]) > 0.1


def test_invalid_audio_rejected(tmp_path):
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not audio")
    with pytest.raises(ValueError):
        probe_audio(path)
    with pytest.raises(ValueError):
        resample_audio(np.array([np.nan]), 48000)
    with pytest.raises(ValueError):
        resample_audio(np.zeros((3, 2)), 48000)


def test_live_probe_limit_can_extend_but_file_decode_stays_ten_minutes(tmp_path):
    path = tmp_path / "long-recording.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(bytes(8000 * 601 * 2))
    with pytest.raises(ValueError, match="10 minutos"):
        probe_audio(path)
    assert probe_audio(path, max_duration_ms=905000)["duration_ms"] == 601000
    with pytest.raises(ValueError, match="10 minutos"):
        decode_audio(path)


def test_nonseekable_browser_webm_duration_and_stereo_preserved(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is required for browser WebM integration tests")
    rate = 48000
    timebase = np.arange(rate * 2) / rate
    caller = 0.3 * np.sin(2 * np.pi * 440 * timebase)
    caller[:rate] = 0
    assistant = 0.3 * np.sin(2 * np.pi * 880 * timebase)
    wav_path = tmp_path / "source.wav"
    write_wav(wav_path, np.column_stack([caller, assistant]), rate)
    # A live, non-seekable WebM has no container Duration element, just like
    # Chrome's MediaRecorder output. Silence on channel 0 must still count.
    result = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(wav_path),
        "-c:a", "libopus", "-live", "1", "-f", "webm", "pipe:1",
    ], capture_output=True, timeout=20, check=True)
    webm_path = tmp_path / "browser-recording.webm"
    webm_path.write_bytes(result.stdout)
    raw_metadata = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "stream=duration:format=duration",
        "-of", "json", str(webm_path),
    ], capture_output=True, timeout=20, check=True)
    metadata = json.loads(raw_metadata.stdout)
    assert "duration" not in metadata["streams"][0]
    assert "duration" not in metadata["format"]
    assert probe_audio(webm_path) == {"duration_ms": 2000, "channels": 2, "sample_rate": rate}
    left, right = decode_audio(webm_path, 0), decode_audio(webm_path, 1)
    assert len(left) == len(right) == 32000
    assert np.max(np.abs(left[:15900])) < 0.001
    assert np.std(right[:16000]) > 0.15
    with pytest.raises(ValueError, match="límite"):
        probe_audio(webm_path, max_duration_ms=1000)
    original_bytes = webm_path.read_bytes()
    with pytest.raises(ValueError, match="límite"):
        prepare_recording(webm_path, max_duration_ms=1000)
    assert webm_path.read_bytes() == original_bytes
    finalized = prepare_recording(webm_path)
    assert finalized["channels"] == 2 and finalized["sample_rate"] == rate
    # The indexed container includes Opus packet/pre-skip timing; decoded PCM
    # below must remain sample-for-sample identical despite this small offset.
    assert finalized["duration_ms"] == pytest.approx(2000, abs=40)
    finalized_raw = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(webm_path),
    ], capture_output=True, timeout=20, check=True)
    assert float(json.loads(finalized_raw.stdout)["format"]["duration"]) == pytest.approx(2, abs=.04)
    np.testing.assert_array_equal(decode_audio(webm_path, 0), left)
    np.testing.assert_array_equal(decode_audio(webm_path, 1), right)
    assert not list(tmp_path.glob(".finalizing-*"))


def test_unknown_duration_unsupported_codec_rejected_before_decode(tmp_path, monkeypatch):
    from backend import audio
    path = tmp_path / "unsupported.webm"
    path.write_bytes(b"placeholder")
    monkeypatch.setattr(audio, "_run", lambda *args, **kwargs: json.dumps({
        "streams": [{"channels": 2, "sample_rate": "48000", "codec_name": "unsupported"}], "format": {},
    }).encode())
    monkeypatch.setattr(audio, "_decode_pcm", lambda *args, **kwargs: pytest.fail("Invalid codec must not be decoded"))
    with pytest.raises(ValueError, match="códec"):
        probe_audio(path)
