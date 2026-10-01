"""Local audio decoding with explicit channel isolation and a 16 kHz timebase."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import numpy as np

SAMPLE_RATE = 16_000
MAX_DURATION_MS = 600_000


def _file(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ValueError("El archivo de audio no existe.")
    return resolved


def _binary(name: str) -> str:
    candidate = os.environ.get(f"{name.upper()}_BINARY", name)
    found = shutil.which(candidate)
    if not found:
        raise RuntimeError(f"Falta {name}. Instala FFmpeg y vuelve a iniciar el servidor.")
    return found


def ffmpeg_available() -> bool:
    try:
        _binary("ffmpeg")
        _binary("ffprobe")
        return True
    except RuntimeError:
        return False


def _run(args: list[str], timeout: int) -> bytes:
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("El archivo tardó demasiado en decodificarse.") from exc
    except OSError as exc:
        raise RuntimeError("No se pudo ejecutar FFmpeg.") from exc
    if result.returncode:
        # FFmpeg errors may contain paths; do not expose subprocess output to clients.
        raise ValueError("El archivo no contiene audio válido o está dañado.")
    return result.stdout


def _decode_pcm(source: Path, channel: int, max_duration_ms: int, pcm_format: str = "f32le") -> bytes:
    # One second beyond the limit lets callers detect oversized files instead of
    # accepting a silently truncated recording. Fixed mono/16 kHz output
    # bounds stdout to 64,000 bytes per second (32,000 for duration-only PCM16).
    return _run([
        _binary("ffmpeg"), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-i", str(source), "-map", "0:a:0", "-vn", "-af", f"pan=mono|c0=c{channel}",
        "-ar", str(SAMPLE_RATE), "-ac", "1", "-t", f"{(max_duration_ms + 1000) / 1000:.3f}",
        "-f", pcm_format, "pipe:1",
    ], timeout=120)


def probe_audio(path: str | Path, max_duration_ms: int = MAX_DURATION_MS) -> dict:
    """Inspect the first audio stream without relying on the upload's extension."""
    if isinstance(max_duration_ms, bool) or not isinstance(max_duration_ms, int) or not 0 < max_duration_ms <= 3_600_000:
        raise ValueError("El límite de duración de audio no es válido.")
    source = _file(path)
    raw = _run([
        _binary("ffprobe"), "-v", "error", "-protocol_whitelist", "file,pipe",
        "-select_streams", "a:0", "-show_entries",
        "stream=channels,sample_rate,duration,codec_name:format=duration", "-of", "json",
        str(source),
    ], timeout=20)
    try:
        metadata = json.loads(raw)
        stream = metadata["streams"][0]
        channels = int(stream["channels"])
        sample_rate = int(stream["sample_rate"])
        codec = stream["codec_name"]
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("No se pudieron determinar los canales y la frecuencia del audio.") from exc
    if channels < 1 or sample_rate < 1:
        raise ValueError("El archivo no contiene audio válido.")
    if not isinstance(codec, str) or (codec not in {"mp3", "aac", "opus", "vorbis"} and not codec.startswith(("pcm_", "adpcm_"))):
        raise ValueError("El códec de audio no es compatible. Usa WAV, MP3, M4A o WebM.")
    duration = None
    for candidate in (stream.get("duration"), metadata.get("format", {}).get("duration")):
        try:
            parsed = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            duration = parsed
            break
    if duration is None:
        # Browser MediaRecorder emits non-seekable WebM without a Duration
        # element. Count decoded samples, including silence, without changing
        # the stream's reported channel count or its original sample rate.
        raw_pcm = _decode_pcm(source, channel=0, max_duration_ms=max_duration_ms, pcm_format="s16le")
        if not raw_pcm or len(raw_pcm) % 2:
            raise ValueError("No se pudieron obtener muestras de audio válidas.")
        duration = len(raw_pcm) / (2 * SAMPLE_RATE)
    if duration * 1000 > max_duration_ms:
        raise ValueError(f"El audio supera el límite de {max_duration_ms // 60000} minutos.")
    return {"duration_ms": round(duration * 1000), "channels": channels, "sample_rate": sample_rate}


def decode_audio(path: str | Path, channel: int = 0) -> np.ndarray:
    """Select one channel BEFORE resampling: the other voice is never downmixed."""
    source = _file(path)
    metadata = probe_audio(source)
    if metadata["duration_ms"] > MAX_DURATION_MS:
        raise ValueError("El audio supera el límite de 10 minutos.")
    if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel < metadata["channels"]:
        raise ValueError("El canal seleccionado no existe en la grabación.")
    raw = _decode_pcm(source, channel, MAX_DURATION_MS)
    samples = np.frombuffer(raw, dtype="<f4").copy()
    if len(samples) > SAMPLE_RATE * MAX_DURATION_MS // 1000:
        raise ValueError("El audio supera el límite de 10 minutos.")
    if not len(samples) or not np.isfinite(samples).all():
        raise ValueError("No se pudieron obtener muestras de audio válidas.")
    return np.clip(samples, -1, 1).astype(np.float32, copy=False)


def prepare_recording(path: str | Path, max_duration_ms: int = MAX_DURATION_MS) -> dict:
    """Finalize a browser WebM container for seekable playback without re-encoding.

    Replace the original only after validating the completed file. Audio packets,
    sample rate and channel separation survive the stream copy unchanged.
    """
    source = _file(path)
    original_metadata = probe_audio(source, max_duration_ms)
    if source.suffix.lower() != ".webm":
        return original_metadata
    with tempfile.NamedTemporaryFile(dir=source.parent, prefix=".finalizing-", suffix=".webm", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        _run([
            _binary("ffmpeg"), "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe",
            "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "copy", "-f", "webm", str(temporary),
        ], timeout=120)
        finalized_metadata = probe_audio(temporary, max_duration_ms)
        if (finalized_metadata["channels"] != original_metadata["channels"]
                or finalized_metadata["sample_rate"] != original_metadata["sample_rate"]):
            raise ValueError("No se pudo conservar la separación de canales de la grabación.")
        os.replace(temporary, source)
        return finalized_metadata
    finally:
        temporary.unlink(missing_ok=True)


def resample_audio(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Resample mono PCM without deleting silence or changing its duration."""
    values = np.asarray(samples, dtype=np.float32)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("Se esperaba audio mono con muestras finitas.")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 192_000:
        raise ValueError("La frecuencia de muestreo no es compatible.")
    if not len(values) or sample_rate == SAMPLE_RATE:
        return np.clip(values, -1, 1).astype(np.float32, copy=True)
    from scipy.signal import resample_poly

    divisor = math.gcd(sample_rate, SAMPLE_RATE)
    converted = resample_poly(values, SAMPLE_RATE // divisor, sample_rate // divisor)
    return np.clip(converted, -1, 1).astype(np.float32, copy=False)
