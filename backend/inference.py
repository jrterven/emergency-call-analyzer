"""Lazy, local-only speech and emotion inference. Importing never downloads models."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

import numpy as np

from .audio import SAMPLE_RATE

EMOTION_LABELS = ("angry", "disgusted", "fearful", "happy", "neutral", "other", "sad", "surprised", "unknown")
VAD_FRAME_MS = 20
VAD_MODE = 1
MIN_EMOTION_VOICED_MS = 1_000
logger = logging.getLogger(__name__)
_whisper = None
_whisper_revision = None
_emotion = None
_emotion_revision = None
_load_lock = threading.RLock()
_inference_lock = threading.RLock()


def _cache_dir() -> Path:
    default = Path(__file__).resolve().parent.parent / ".model-cache"
    return Path(os.environ.get("MODEL_CACHE_DIR", str(default))).resolve()


def _cpu_threads() -> int:
    try:
        requested = int(os.environ.get("TORCH_NUM_THREADS", "4"))
    except ValueError:
        requested = 4
    return min(4, max(1, requested), max(1, os.cpu_count() or 1))


def _torch():
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("Falta PyTorch. Instala requirements-ml.txt.") from exc
    torch.set_num_threads(_cpu_threads())
    return torch


def _get_whisper():
    global _whisper, _whisper_revision
    with _load_lock:
        if _whisper is None:
            try:
                import whisper

                _torch()
                cache = _cache_dir() / "whisper"
                cache.mkdir(parents=True, exist_ok=True)
                model_name = os.environ.get("WHISPER_MODEL", "small")
                _whisper = whisper.load_model(model_name, device="cpu", download_root=str(cache))
                model_url = getattr(whisper, "_MODELS", {}).get(model_name, "")
                _whisper_revision = model_url.split("/")[-2] if model_url else None
            except Exception as exc:
                logger.exception("Local Whisper loading failed")
                raise RuntimeError(
                    "No se pudo cargar Whisper local. Comprueba las dependencias y la descarga inicial de pesos."
                ) from exc
        return _whisper


def _get_emotion():
    global _emotion, _emotion_revision
    with _load_lock:
        if _emotion is None:
            try:
                from huggingface_hub import snapshot_download
                from funasr import AutoModel

                _torch()
                model_name = os.environ.get("EMOTION_MODEL", "emotion2vec/emotion2vec_plus_base")
                requested_revision = os.environ.get("EMOTION_MODEL_REVISION") or "main"
                options = {
                    "repo_id": model_name,
                    "revision": requested_revision,
                    "cache_dir": str(_cache_dir() / "huggingface"),
                    "allow_patterns": ["config.yaml", "configuration.json", "model.pt", "tokens.txt", "tokens.json"],
                }
                # Reuse cached snapshots without requiring connectivity or a model hub update.
                try:
                    model_path = snapshot_download(**options, local_files_only=True)
                    if not all((Path(model_path) / name).is_file() for name in ("model.pt", "config.yaml")):
                        raise FileNotFoundError("Incomplete local emotion snapshot")
                except (FileNotFoundError, OSError):
                    model_path = snapshot_download(**options)
                _emotion = AutoModel(
                    model=model_path, hub="hf", device="cpu", ngpu=0, ncpu=_cpu_threads(),
                    disable_update=True, disable_pbar=True, trust_remote_code=False,
                    log_level="WARNING",
                )
                resolved = Path(model_path).name
                _emotion_revision = resolved if re.fullmatch(r"[0-9a-f]{40}", resolved) else requested_revision
            except Exception as exc:
                logger.exception("Local emotion2vec+ loading failed")
                raise RuntimeError(
                    "No se pudo cargar emotion2vec+ local. Comprueba las dependencias y la descarga inicial de pesos."
                ) from exc
        return _emotion


def _samples(samples: np.ndarray) -> np.ndarray:
    values = np.asarray(samples, dtype=np.float32)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("Se esperaba audio mono de 16 kHz con muestras finitas.")
    return np.ascontiguousarray(np.clip(values, -1, 1))


def _voiced_frames(samples: np.ndarray) -> np.ndarray:
    """WebRTC VAD over complete 20 ms frames; quiet recordings never load an ML model."""
    frame_samples = SAMPLE_RATE * VAD_FRAME_MS // 1000
    count = len(samples) // frame_samples
    if not count:
        return np.zeros(0, dtype=bool)
    try:
        import webrtcvad
    except ImportError as exc:
        raise RuntimeError("Falta WebRTC VAD. Instala requirements-ml.txt.") from exc
    detector = webrtcvad.Vad(VAD_MODE)
    pcm = (np.clip(samples[:count * frame_samples], -1, 1) * 32767).astype("<i2")
    return np.array([
        detector.is_speech(pcm[offset:offset + frame_samples].tobytes(), SAMPLE_RATE)
        for offset in range(0, len(pcm), frame_samples)
    ], dtype=bool)


def _scores(result: dict) -> dict[str, float]:
    """Normalize bilingual class names, preserving the model's original nine scores."""
    labels, values = result.get("labels", []), result.get("scores", [])
    if len(labels) != 9 or len(values) != 9:
        raise ValueError("El modelo emocional no devolvió sus nueve categorías.")
    mapped = {}
    aliases = {"<unk>": "unknown", "unk": "unknown", "未知": "unknown"}
    for raw_label, raw_value in zip(labels, values):
        parts = str(raw_label).lower().strip().split("/")
        label = next((part.strip() for part in parts if part.strip() in EMOTION_LABELS), None)
        if label is None:
            label = aliases.get(str(raw_label).lower().strip())
        score = float(raw_value)
        if label is None or label in mapped or not np.isfinite(score):
            raise ValueError("El modelo emocional devolvió categorías o puntuaciones incompatibles.")
        mapped[label] = score
    if set(mapped) != set(EMOTION_LABELS):
        raise ValueError("Las categorías del modelo emocional son incompatibles.")
    return {label: mapped[label] for label in EMOTION_LABELS}


def analyze_emotion(samples: np.ndarray, start_ms: int, end_ms: int) -> dict:
    began = time.perf_counter()
    window = {
        "id": str(uuid.uuid4()), "start_ms": int(start_ms), "end_ms": int(end_ms),
        "scores": {}, "label": None, "status": "insufficient_audio", "latency_ms": 0, "voiced_ms": 0,
    }
    try:
        values = _samples(samples)
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("Los tiempos de la ventana de audio no son válidos.")
        window["voiced_ms"] = int(np.count_nonzero(_voiced_frames(values)) * VAD_FRAME_MS)
        if window["voiced_ms"] >= MIN_EMOTION_VOICED_MS:
            with _inference_lock:
                model = _get_emotion()
                with _torch().inference_mode():
                    results = model.generate(
                        input=values, fs=SAMPLE_RATE, granularity="utterance",
                        extract_embedding=False, disable_pbar=True,
                    )
            if not isinstance(results, list) or not results:
                raise ValueError("El modelo emocional no devolvió resultados.")
            scores = _scores(results[0])
            window.update(scores=scores, label=max(scores, key=scores.get), status="ok")
    except Exception as exc:
        logger.exception("Emotion window inference failed")
        window.update(status="error", error=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Falló el análisis emocional local.")
    window["latency_ms"] = round((time.perf_counter() - began) * 1000)
    return window


def transcribe_audio(samples: np.ndarray) -> list[dict]:
    values = _samples(samples)
    frames = _voiced_frames(values)
    if not np.any(frames):
        return []
    try:
        with _inference_lock:
            model = _get_whisper()
            with _torch().inference_mode():
                result = model.transcribe(
                    values, language="es", task="transcribe", fp16=False, verbose=None,
                    temperature=0, condition_on_previous_text=False,
                    no_speech_threshold=0.6, logprob_threshold=-1.0,
                )
    except Exception as exc:
        logger.exception("Local Whisper transcription failed")
        raise RuntimeError("Falló la transcripción con Whisper local. No se envió audio a una API.") from exc
    duration_ms = round(len(values) * 1000 / SAMPLE_RATE)
    fragments = []
    for segment in result.get("segments", []):
        text = str(segment.get("text", "")).strip()
        start_ms = min(duration_ms, max(0, round(float(segment["start"]) * 1000)))
        end_ms = min(duration_ms, max(start_ms, round(float(segment["end"]) * 1000)))
        start_frame, end_frame = start_ms // VAD_FRAME_MS, (end_ms + VAD_FRAME_MS - 1) // VAD_FRAME_MS
        # Suppress silence-only segments without removing silence from the model's input timebase.
        if not text or not np.any(frames[start_frame:end_frame]):
            continue
        fragments.append({
            "id": str(uuid.uuid4()), "speaker": "caller", "text": text,
            "start_ms": start_ms, "end_ms": end_ms,
        })
    return fragments


def get_model_status() -> dict[str, bool]:
    return {"whisper": _whisper is not None, "emotion": _emotion is not None}


def get_model_metadata() -> dict:
    def installed(name: str):
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    return {
        "whisper": os.environ.get("WHISPER_MODEL", "small"),
        "whisper_revision": _whisper_revision, "whisper_version": installed("openai-whisper"),
        "emotion": os.environ.get("EMOTION_MODEL", "emotion2vec/emotion2vec_plus_base"),
        "emotion_revision": _emotion_revision or os.environ.get("EMOTION_MODEL_REVISION") or "main (sin cargar)",
        "funasr_version": installed("funasr"), "device": "cpu", "cpu_threads": _cpu_threads(), "sample_rate": SAMPLE_RATE,
        "emotion_window_ms": 4_000, "emotion_hop_ms": 2_000,
        "vad": {"model": "webrtcvad", "mode": VAD_MODE, "frame_ms": VAD_FRAME_MS, "min_voiced_ms": MIN_EMOTION_VOICED_MS},
        "emotion_score_kind": "model_scores", "transcript_timestamps": "estimated", "whisper_language": "es",
    }
