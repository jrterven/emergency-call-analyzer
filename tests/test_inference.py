from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

from backend import inference


def test_silence_and_short_speech_do_not_load_emotion(monkeypatch):
    monkeypatch.setattr(inference, "_voiced_frames", lambda audio: np.zeros(len(audio) // 320, dtype=bool))
    monkeypatch.setattr(inference, "_get_emotion", lambda: pytest.fail("Silence must not load emotion weights"))
    silent = inference.analyze_emotion(np.zeros(64000, dtype=np.float32), 4000, 8000)
    assert silent["status"] == "insufficient_audio"
    assert silent["label"] is None and silent["scores"] == {} and silent["voiced_ms"] == 0
    monkeypatch.setattr(inference, "_voiced_frames", lambda audio: np.ones(40, dtype=bool))
    assert inference.analyze_emotion(np.ones(64000, dtype=np.float32) * 0.1, 0, 4000)["status"] == "insufficient_audio"


def test_original_bilingual_scores_retained_without_normalization(monkeypatch):
    labels = ["生气/angry", "厌恶/disgusted", "恐惧/fearful", "开心/happy", "中性/neutral", "其他/other", "难过/sad", "吃惊/surprised", "<unk>"]
    scores = [0.03, 0.02, 0.4, 0.03, 0.12, 0.02, 0.1, 0.07, 0.01]
    calls = []
    model = SimpleNamespace(generate=lambda **kwargs: calls.append(kwargs) or [{"labels": labels, "scores": scores}])
    monkeypatch.setattr(inference, "_voiced_frames", lambda audio: np.ones(70, dtype=bool))
    monkeypatch.setattr(inference, "_get_emotion", lambda: model)
    monkeypatch.setattr(inference, "_torch", lambda: SimpleNamespace(inference_mode=nullcontext))
    result = inference.analyze_emotion(np.ones(64000, dtype=np.float32) * 0.1, 2000, 6000)
    assert result["status"] == "ok" and result["label"] == "fearful"
    assert result["scores"] == dict(zip(inference.EMOTION_LABELS, scores))
    assert calls[0]["extract_embedding"] is False
    assert result["voiced_ms"] == 1400


def test_model_failure_is_explicit_window(monkeypatch):
    monkeypatch.setattr(inference, "_voiced_frames", lambda audio: np.ones(100, dtype=bool))
    monkeypatch.setattr(inference, "_get_emotion", lambda: (_ for _ in ()).throw(RuntimeError("Pesos no disponibles")))
    result = inference.analyze_emotion(np.ones(64000, dtype=np.float32), 0, 4000)
    assert result["status"] == "error" and result["error"] == "Pesos no disponibles"
    assert result["scores"] == {}


def test_whisper_silence_never_hallucinates_or_downloads(monkeypatch):
    monkeypatch.setattr(inference, "_voiced_frames", lambda audio: np.zeros(100, dtype=bool))
    monkeypatch.setattr(inference, "_get_whisper", lambda: pytest.fail("Silent file must not load Whisper"))
    assert inference.transcribe_audio(np.zeros(32000, dtype=np.float32)) == []


def test_whisper_preserves_timestamps_and_suppresses_silence_segments(monkeypatch):
    calls = []
    def transcribe(audio, **kwargs):
        calls.append((audio.copy(), kwargs))
        return {"segments": [
            {"start": 0, "end": 0.8, "text": "Hallucination over silence"},
            {"start": 1.1, "end": 2, "text": " Necesito ayuda. "},
        ]}
    frames = np.zeros(100, dtype=bool)
    frames[50:] = True
    monkeypatch.setattr(inference, "_voiced_frames", lambda audio: frames)
    monkeypatch.setattr(inference, "_get_whisper", lambda: SimpleNamespace(transcribe=transcribe))
    monkeypatch.setattr(inference, "_torch", lambda: SimpleNamespace(inference_mode=nullcontext))
    audio = np.concatenate([np.zeros(16000), np.ones(16000) * 0.1]).astype(np.float32)
    result = inference.transcribe_audio(audio)
    assert len(result) == 1
    assert result[0]["start_ms"] == 1100 and result[0]["end_ms"] == 2000
    assert result[0]["text"] == "Necesito ayuda."
    np.testing.assert_array_equal(calls[0][0], audio)
    assert calls[0][1]["fp16"] is False and calls[0][1]["language"] == "es"


def test_webrtcvad_real_silence():
    pytest.importorskip("webrtcvad")
    frames = inference._voiced_frames(np.zeros(64000, dtype=np.float32))
    assert len(frames) == 200 and not np.any(frames)
