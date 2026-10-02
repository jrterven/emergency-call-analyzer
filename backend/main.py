"""FastAPI orchestration. Heavy local inference runs on one worker thread."""

import asyncio
import csv
import io
import json
import logging
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from urllib.parse import quote

import httpx
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from websockets.asyncio.client import connect as connect_sideband

from .config import Settings, settings
from .models import ConnectSession, CreateSession, FinishSession, IncidentSummary, TranscriptFragment
from .prompts import DELEGATION_PROMPT, LIVE_PROMPT, PROMPT_VERSION, SUMMARY_PROMPT
from .storage import Store

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config: Settings):
        self.settings = config
        self.store = Store(config.data_dir)
        self.active_session_id: str | None = None
        self.slot_lock = asyncio.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-models")
        self.jobs: set[asyncio.Task] = set()
        self.finalizing: set[str] = set()
        self.streams: dict[str, LiveStream] = {}
        self.listeners: dict[str, set[WebSocket]] = {}
        self.send_locks: dict[WebSocket, asyncio.Lock] = {}

    async def local(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, function, *args)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)
        return task

    def launch_finish(self, session_id: str, complete: bool):
        if session_id not in self.finalizing:
            self.finalizing.add(session_id)
            self.spawn(self.finish_live(session_id, complete))

    async def reserve(self, session_id: str | None = None):
        async with self.slot_lock:
            if self.active_session_id is not None:
                raise HTTPException(409, "Ya hay una llamada o análisis activo. Termínalo antes de iniciar otro.")
            self.active_session_id = session_id or "reserved"

    async def release(self, session_id: str):
        async with self.slot_lock:
            if self.active_session_id == session_id:
                self.active_session_id = None

    def session(self, session_id: str) -> dict:
        session = self.store.get(session_id)
        if not session:
            raise HTTPException(404, "Sesión no encontrada")
        return session

    async def send(self, websocket: WebSocket, event: dict):
        try:
            async with self.send_locks.setdefault(websocket, asyncio.Lock()):
                await asyncio.wait_for(websocket.send_json(event), timeout=2)
        except (RuntimeError, OSError, asyncio.TimeoutError, WebSocketDisconnect):
            for listeners in self.listeners.values():
                listeners.discard(websocket)

    async def publish(self, session_id: str, event: dict):
        await asyncio.gather(*(self.send(ws, event) for ws in list(self.listeners.get(session_id, set()))))

    async def changed(self, session_id: str):
        await self.publish(session_id, {"type": "session", "session": self.session(session_id)})

    async def warning(self, session_id: str, message: str):
        def add(session):
            if message not in session["warnings"]:
                session["warnings"].append(message)
        self.store.mutate(session_id, add)
        await self.publish(session_id, {"type": "warning", "message": message})

    async def emotion(self, session_id: str, result: dict):
        def add(session):
            session["emotions"].append(result)
            session["emotions"].sort(key=lambda window: (window["start_ms"], window["end_ms"]))
        self.store.mutate(session_id, add)
        await self.publish(session_id, {"type": "emotion", "window": result})

    def configuration(self):
        from .inference import get_model_metadata
        return {
            **get_model_metadata(), "live_model": self.settings.live_model,
            "summary_model": self.settings.summary_model, "prompt_version": PROMPT_VERSION,
            "live_prompt": LIVE_PROMPT, "delegation_prompt": DELEGATION_PROMPT,
            "summary_prompt": SUMMARY_PROMPT, "language": "es-MX",
            "window_ms": self.settings.window_ms, "hop_ms": self.settings.hop_ms,
            "max_live_duration_ms": self.settings.max_live_duration_ms,
            "emotion_interpretation": "Puntuaciones del modelo; no probabilidades de veracidad",
            "transcript_timestamps": "Whisper: segmentos estimados; GPT-Live: tiempos originales",
        }

    def refresh_model_metadata(self, session_id: str):
        from .inference import get_model_metadata
        def refresh(session):
            metadata = get_model_metadata()
            metadata["transcript_timestamps"] = "gpt_live_original" if session["source"] == "live" else "whisper_estimated"
            session["config"].update(metadata)
        self.store.mutate(session_id, refresh)

    async def close_late_provider_session(self, provider_session_id: str):
        """Close a canceled WebRTC creation without touching local session state."""
        async def close():
            # Documented GPT-Live sideband supports existing WebRTC sessions.
            # The provider ID came from this server's authenticated create call.
            url = f"wss://api.openai.com/v1/live/sessions/{quote(provider_session_id, safe='')}/attach"
            async with connect_sideband(url, additional_headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
            }, open_timeout=3, close_timeout=1, max_size=1_048_576) as sideband:
                await sideband.send(json.dumps({"type": "session.close", "event_id": str(uuid4())}))
                async for raw in sideband:
                    event = json.loads(raw)
                    if event.get("type") == "session.closed":
                        return
                    if event.get("type") == "error":
                        raise RuntimeError("Provider rejected close")
                raise RuntimeError("Provider closed without confirmation")
        try:
            await asyncio.wait_for(close(), timeout=5)
        except Exception:
            # No provider bodies, session credentials or API key enter the logs.
            logger.warning("No se confirmó el cierre remoto de una conexión GPT-Live cancelada dentro del límite de tiempo.")

    async def summarize(self, session_id: str):
        session = self.session(session_id)
        separator = "" if session["source"] == "live" else "\n"
        text = separator.join(fragment["text"] for fragment in session["transcript"] if fragment["speaker"] == "caller")
        if not text.strip():
            await self.warning(session_id, "No hay declaraciones del llamante para generar un resumen.")
            return
        if not self.settings.openai_api_key:
            await self.warning(session_id, "Resumen pendiente: configura OPENAI_API_KEY. El audio, las emociones y la transcripción permanecen locales.")
            return
        self.store.mutate(session_id, lambda s: s.update(stage="Generando resumen"))
        await self.changed(session_id)
        schema = IncidentSummary.model_json_schema()
        schema["additionalProperties"] = False
        payload = {
            "model": self.settings.summary_model, "instructions": SUMMARY_PROMPT,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
            "text": {"format": {"type": "json_schema", "name": "incident_summary", "strict": True, "schema": schema}},
        }
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post("https://api.openai.com/v1/responses", json=payload,
                                             headers={"Authorization": f"Bearer {self.settings.openai_api_key}"})
            response.raise_for_status()
            body = response.json()
            output = "".join(part.get("text", "") for item in body.get("output", [])
                             if item.get("type") == "message" for part in item.get("content", [])
                             if part.get("type") == "output_text")
            summary = IncidentSummary.model_validate_json(output).model_dump()
            self.store.mutate(session_id, lambda s: s.update(summary=summary))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            await self.warning(session_id, "No se pudo generar el resumen. Puedes reintentarlo; los resultados locales están conservados.")

    async def process_file(self, session_id: str, original: Path, channel: int):
        from .audio import decode_audio
        from .inference import analyze_emotion, transcribe_audio
        failures = False
        try:
            self.store.mutate(session_id, lambda s: s.update(stage="Preparando audio"))
            samples = await self.local(decode_audio, original, channel)
            normalized = self.store.session_dir(session_id) / "normalized.wav"
            await self.local(write_wav, normalized, samples, 16_000)
            duration_ms = round(len(samples) / 16)
            self.store.mutate(session_id, lambda s: s.update(
                duration_ms=duration_ms, audio_url=f"/api/sessions/{session_id}/audio",
                stage="Analizando emociones",
            ))
            await self.changed(session_id)
            for start_ms, end_ms in window_bounds(duration_ms, self.settings.window_ms, self.settings.hop_ms):
                result = await self.local(analyze_emotion, samples[start_ms * 16:end_ms * 16], start_ms, end_ms)
                await self.emotion(session_id, result)
                if result["status"] == "error":
                    failures = True
                    await self.warning(session_id, result.get("error", "El modelo emocional no está disponible."))
                    # An unavailable model should not trigger hundreds of downloads/retries.
                    break
            self.store.mutate(session_id, lambda s: s.update(stage="Transcribiendo con Whisper local"))
            await self.changed(session_id)
            try:
                transcript = await self.local(transcribe_audio, samples)
                self.store.mutate(session_id, lambda s: s.update(transcript=transcript))
            except Exception:
                failures = True
                await self.warning(session_id, "Whisper local no pudo transcribir. Revisa las dependencias y la descarga del modelo; no se usó una API de transcripción.")
            self.refresh_model_metadata(session_id)
            await self.summarize(session_id)
            self.store.mutate(session_id, lambda s: s.update(status="partial" if failures else "completed", stage="Resultados parciales" if failures else "Completada"))
        except Exception:
            self.store.mutate(session_id, lambda s: s.update(status="failed", stage="No se pudo procesar el audio"))
            await self.warning(session_id, "Falló la preparación del archivo. El original y los resultados disponibles se conservaron.")
        finally:
            await self.release(session_id)
            await self.changed(session_id)

    async def finish_live(self, session_id: str, complete: bool):
        stream = self.streams.get(session_id)
        connection_failed = self.session(session_id)["status"] == "failed"
        try:
            if stream:
                await stream.finish()
            self.refresh_model_metadata(session_id)
            await self.summarize(session_id)
            self.store.mutate(session_id, lambda s: s.update(status="failed" if connection_failed else ("completed" if complete else "partial"), stage="Completada" if complete else "Interrumpida; resultados conservados"))
        except Exception:
            self.store.mutate(session_id, lambda s: s.update(status="partial", stage="Resultados parciales"))
            await self.warning(session_id, "No se pudo completar el cierre. Se conservaron los resultados disponibles.")
        finally:
            self.streams.pop(session_id, None)
            self.finalizing.discard(session_id)
            await self.release(session_id)
            await self.changed(session_id)


def write_wav(path: Path, samples: np.ndarray, sample_rate: int):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def window_bounds(duration_ms: int, window_ms: int = 4000, hop_ms: int = 2000):
    """Keep a final, incomplete window on the same clock without trimming silences."""
    start = 0
    while start < duration_ms:
        end = min(start + window_ms, duration_ms)
        yield start, end
        if end == duration_ms:
            break
        start += hop_ms


class LiveStream:
    def __init__(self, runtime: Runtime, session_id: str, sample_rate: int):
        self.runtime, self.session_id, self.sample_rate = runtime, session_id, sample_rate
        self.received = 0
        self.ring_start = 0
        self.ring = np.empty(0, dtype=np.float32)
        self.window_samples = sample_rate * runtime.settings.window_ms // 1000
        self.hop_samples = sample_rate * runtime.settings.hop_ms // 1000
        self.next_end = self.window_samples
        self.last_end = 0
        self.pending: tuple[np.ndarray, int, int] | None = None
        self.ready = asyncio.Event()
        self.accepting = True
        self.closing = False
        self.finished = False
        self.inference_failed = False
        self.output = wave.open(str(runtime.store.session_dir(session_id) / "caller.wav"), "wb")
        self.output.setnchannels(1)
        self.output.setsampwidth(2)
        self.output.setframerate(sample_rate)
        self.worker = runtime.spawn(self._run())

    async def push(self, data: bytes):
        if not self.accepting:
            return
        if len(data) % 2 or len(data) > self.sample_rate * 2:
            raise ValueError("Los paquetes deben contener PCM16 mono de hasta un segundo.")
        incoming = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
        if (self.received + len(incoming)) * 1000 / self.sample_rate > self.runtime.settings.max_live_duration_ms:
            raise ValueError("La simulación alcanzó el límite de 15 minutos. Finaliza la llamada.")
        # Patch the WAV header on each packet so a restart can recover received audio.
        self.output.writeframes(data)
        self.ring = np.concatenate((self.ring, incoming))
        self.received += len(incoming)
        while self.received >= self.next_end:
            start = self.next_end - self.window_samples
            samples = self.ring[start - self.ring_start:self.next_end - self.ring_start].copy()
            await self._schedule(samples, start, self.next_end)
            self.last_end = self.next_end
            self.next_end += self.hop_samples
            duration_ms = round(self.received * 1000 / self.sample_rate)
            self.runtime.store.mutate(self.session_id, lambda s: s.update(duration_ms=duration_ms))
        keep_start = max(0, self.next_end - self.window_samples)
        discard = max(0, keep_start - self.ring_start)
        self.ring = self.ring[discard:]
        self.ring_start += discard

    async def _schedule(self, samples, start, end):
        start_ms, end_ms = round(start * 1000 / self.sample_rate), round(end * 1000 / self.sample_rate)
        previous = self.pending
        # Swap before yielding: a worker must never process a window marked skipped.
        self.pending = samples, start_ms, end_ms
        self.ready.set()
        if previous:
            _, old_start, old_end = previous
            await self.runtime.emotion(self.session_id, {
                "id": str(uuid4()), "start_ms": old_start, "end_ms": old_end,
                "scores": {}, "label": None, "status": "skipped", "latency_ms": 0, "voiced_ms": 0,
            })

    async def _run(self):
        from .audio import resample_audio
        from .inference import analyze_emotion
        while True:
            await self.ready.wait()
            self.ready.clear()
            item, self.pending = self.pending, None
            if item is not None:
                samples, start_ms, end_ms = item
                if not self.inference_failed:
                    def infer():
                        normalized = resample_audio(samples, self.sample_rate)
                        return analyze_emotion(normalized, start_ms, end_ms)
                    try:
                        result = await self.runtime.local(infer)
                    except Exception:
                        result = {"id": str(uuid4()), "start_ms": start_ms, "end_ms": end_ms,
                                  "scores": {}, "label": None, "status": "error", "latency_ms": 0,
                                  "voiced_ms": 0, "error": "El análisis emocional local falló. La conversación continúa."}
                    await self.runtime.emotion(self.session_id, result)
                    if result["status"] == "error":
                        self.inference_failed = True
                        await self.runtime.warning(self.session_id, result.get("error", "El modelo emocional no está disponible."))
                else:
                    await self.runtime.emotion(self.session_id, {
                        "id": str(uuid4()), "start_ms": start_ms, "end_ms": end_ms,
                        "scores": {}, "label": None, "status": "skipped", "latency_ms": 0, "voiced_ms": 0,
                    })
            if self.closing and self.pending is None:
                break

    async def finish(self):
        if self.finished:
            return
        self.closing = True
        self.accepting = False
        if self.received > self.last_end:
            start = max(0, self.last_end - self.window_samples + self.hop_samples) if self.last_end else 0
            start = max(start, self.ring_start)
            await self._schedule(self.ring[start - self.ring_start:].copy(), start, self.received)
        self.output.close()
        duration_ms = round(self.received * 1000 / self.sample_rate)
        self.runtime.store.mutate(self.session_id, lambda s: s.update(
            duration_ms=max(s["duration_ms"], duration_ms), audio_url=f"/api/sessions/{self.session_id}/audio",
        ))
        self.ready.set()
        await self.worker
        self.finished = True


def create_app(config: Settings = settings) -> FastAPI:
    runtime = Runtime(config)

    @asynccontextmanager
    async def lifespan(app):
        runtime.store.initialize()
        yield
        for stream in list(runtime.streams.values()):
            stream.output.close()
        for job in runtime.jobs:
            job.cancel()
        await asyncio.gather(*runtime.jobs, return_exceptions=True)
        runtime.executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Emergency Analyzer", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(CORSMiddleware, allow_origins=list(config.allowed_origins), allow_methods=["GET", "POST", "DELETE"], allow_headers=["Content-Type"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in config.allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "Origen no permitido. Usa esta aplicación en localhost."})
        limit = config.max_recording_bytes if request.url.path.endswith("/recording") else config.max_upload_bytes + 65_536
        try:
            if int(request.headers.get("content-length", "0")) > limit:
                return JSONResponse(status_code=413, content={"detail": "El archivo supera el límite permitido."})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Tamaño de petición inválido"})
        return await call_next(request)

    @app.get("/api/health")
    async def health():
        from .audio import ffmpeg_available
        from .inference import get_model_status
        return {"openai_configured": bool(config.openai_api_key), "ffmpeg_available": ffmpeg_available(),
                "whisper_model": config.whisper_model, "emotion_model": config.emotion_model,
                "active_session_id": runtime.active_session_id, "models_loaded": get_model_status()}

    @app.get("/api/sessions")
    async def sessions():
        return runtime.store.list()

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str):
        return runtime.session(session_id)

    @app.post("/api/sessions", status_code=201)
    async def create(body: CreateSession):
        await runtime.reserve()
        try:
            result = runtime.store.create(body.source, body.title, runtime.configuration())
            runtime.active_session_id = result["id"]
            return result
        except Exception:
            runtime.active_session_id = None
            raise

    @app.post("/api/sessions/{session_id}/connect", status_code=201)
    async def connect(session_id: str, body: ConnectSession):
        session = runtime.session(session_id)
        if session["source"] != "live" or session["status"] != "created" or runtime.active_session_id != session_id:
            raise HTTPException(409, "La sesión no está disponible para conectar.")
        if not config.openai_api_key:
            raise HTTPException(503, "Configura OPENAI_API_KEY en el servidor para realizar una llamada simulada.")
        if not body.sdp.startswith("v=0"):
            raise HTTPException(400, "Se requiere una oferta SDP válida.")
        runtime.store.mutate(session_id, lambda s: s.update(status="connecting", stage="Conectando GPT-Live"))
        payload = {"session": {"model": config.live_model, "instructions": LIVE_PROMPT,
                   "delegation": {"type": "responses", "responses": {"model": config.summary_model,
                       "instructions": DELEGATION_PROMPT, "tool_choice": "none"}}},
                   "transport": {"type": "webrtc", "sdp": body.sdp}}
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                response = await client.post("https://api.openai.com/v1/live/sessions", json=payload,
                                            headers={"Authorization": f"Bearer {config.openai_api_key}"})
            response.raise_for_status()
            result = response.json()
            answer = {"session": {"id": result["session"]["id"]}, "transport": {"type": "webrtc", "sdp": result["transport"]["sdp"]}}
            current = runtime.store.get(session_id)
            if not current or current["status"] != "connecting" or runtime.active_session_id != session_id:
                # /finish can run while the upstream creation request is pending.
                # A late answer must never revive or modify the saved session.
                await runtime.close_late_provider_session(answer["session"]["id"])
                raise HTTPException(409, "La llamada se canceló mientras se conectaba.")
            runtime.store.mutate(session_id, lambda s: s["config"].update(live_session_id=answer["session"]["id"]))
            return answer
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            current = runtime.store.get(session_id)
            if not current or current["status"] != "connecting" or runtime.active_session_id != session_id:
                raise HTTPException(409, "La llamada se canceló mientras se conectaba.")
            runtime.store.mutate(session_id, lambda s: s.update(status="failed", stage="No se pudo conectar GPT-Live"))
            current_stream = runtime.streams.get(session_id)
            if current_stream:
                current_stream.accepting = False
                runtime.launch_finish(session_id, False)
            else:
                await runtime.release(session_id)
            raise HTTPException(502, "No se pudo conectar GPT-Live. Revisa la API key, el acceso al modelo y tu conexión.")

    async def save_upload(file: UploadFile, path: Path, limit: int):
        size = 0
        try:
            with path.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "El archivo supera el límite permitido.")
                    output.write(chunk)
            if not size:
                raise HTTPException(400, "El archivo está vacío.")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.post("/api/sessions/{session_id}/upload", status_code=202)
    async def upload(session_id: str, file: UploadFile = File(...), channel: int = Form(0)):
        from .audio import probe_audio
        session = runtime.session(session_id)
        if session["source"] != "file" or session["status"] != "created" or runtime.active_session_id != session_id:
            raise HTTPException(409, "La sesión no está disponible para cargar archivos.")
        filename = Path(file.filename or "audio").name
        extension = Path(filename).suffix.lower()
        if extension not in {".wav", ".mp3", ".m4a", ".webm"}:
            raise HTTPException(400, "Formato no admitido. Usa WAV, MP3, M4A o WebM.")
        original = runtime.store.session_dir(session_id) / ("original" + extension)
        try:
            await save_upload(file, original, config.max_upload_bytes)
            probe = await runtime.local(probe_audio, original)
            if not 0 < probe["duration_ms"] <= config.max_file_duration_ms:
                raise ValueError("La grabación debe durar entre un instante y 10 minutos.")
            if not 0 <= channel < probe["channels"]:
                raise ValueError("El canal seleccionado no existe en esta grabación.")
        except (ValueError, RuntimeError) as error:
            original.unlink(missing_ok=True)
            raise HTTPException(400, str(error))
        runtime.store.mutate(session_id, lambda s: s.update(status="processing", stage="Preparando audio", filename=filename,
                              channel=channel, duration_ms=probe["duration_ms"], original_audio_url=f"/api/sessions/{session_id}/audio?original=true"))
        runtime.store.mutate(session_id, lambda s: s["config"].update(input_audio=probe, original_artifact=original.name))
        runtime.spawn(runtime.process_file(session_id, original, channel))
        return runtime.session(session_id)

    @app.post("/api/sessions/{session_id}/recording")
    async def recording(session_id: str, file: UploadFile = File(...)):
        from .audio import prepare_recording
        session = runtime.session(session_id)
        if session["source"] != "live" or session["status"] not in {"created", "connecting", "live"}:
            raise HTTPException(409, "La sesión ya no acepta una grabación.")
        extension = Path(file.filename or "recording.webm").suffix.lower()
        if extension not in {".wav", ".webm"}:
            raise HTTPException(400, "La grabación debe ser WAV o WebM estéreo.")
        path = runtime.store.session_dir(session_id) / ("recording" + extension)
        await save_upload(file, path, config.max_recording_bytes)
        try:
            probe = await runtime.local(prepare_recording, path, config.max_live_duration_ms + 5000)
            if probe["channels"] != 2 or probe["duration_ms"] > config.max_live_duration_ms + 5000:
                raise ValueError("Se requiere una grabación estéreo de hasta 15 minutos: llamante a la izquierda, asistente a la derecha.")
        except (ValueError, RuntimeError) as error:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(error))
        runtime.store.mutate(session_id, lambda s: s.update(filename=path.name, audio_url=f"/api/sessions/{session_id}/audio",
                              original_audio_url=f"/api/sessions/{session_id}/audio?original=true"))
        runtime.store.mutate(session_id, lambda s: s["config"].update(recording_audio=probe, recording_artifact=path.name))
        return runtime.session(session_id)

    @app.post("/api/sessions/{session_id}/finish", status_code=202)
    async def finish(session_id: str, body: FinishSession):
        session = runtime.session(session_id)
        if session["source"] != "live":
            raise HTTPException(409, "El análisis de archivos termina automáticamente.")
        if session["status"] in {"completed", "partial", "failed", "processing"}:
            if session["status"] == "failed" and session_id in runtime.streams:
                runtime.streams[session_id].accepting = False
                runtime.launch_finish(session_id, False)
            return session
        stream = runtime.streams.get(session_id)
        if stream:
            stream.accepting = False
        runtime.store.mutate(session_id, lambda s: s.update(status="processing", stage="Finalizando análisis", duration_ms=body.duration_ms))
        if body.usage:
            runtime.store.mutate(session_id, lambda s: s["config"].update(live_usage=body.usage))
        artifact_name = Path(session["config"].get("recording_artifact", "recording.wav")).name
        if body.complete and not (runtime.store.session_dir(session_id) / artifact_name).is_file():
            await runtime.warning(session_id, "La grabación estéreo no está disponible; se conserva el audio del llamante recibido por el backend.")
        runtime.launch_finish(session_id, body.complete)
        return runtime.session(session_id)

    @app.post("/api/sessions/{session_id}/summary", status_code=202)
    async def retry_summary(session_id: str):
        session = runtime.session(session_id)
        if session["status"] not in {"completed", "partial", "failed"}:
            raise HTTPException(409, "Espera a que termine la sesión.")
        if not config.openai_api_key:
            raise HTTPException(503, "Configura OPENAI_API_KEY para generar el resumen.")
        await runtime.reserve(session_id)
        previous_status = session["status"]
        runtime.store.mutate(session_id, lambda s: s.update(status="processing", stage="Generando resumen"))
        async def retry():
            try:
                await runtime.summarize(session_id)
            finally:
                runtime.store.mutate(session_id, lambda s: s.update(status=previous_status, stage="Completada" if previous_status == "completed" else "Resultados parciales"))
                await runtime.release(session_id)
                await runtime.changed(session_id)
        runtime.spawn(retry())
        return runtime.session(session_id)

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def delete(session_id: str):
        session = runtime.session(session_id)
        if session_id in runtime.streams or runtime.listeners.get(session_id) or (runtime.active_session_id == session_id and session["status"] != "created"):
            raise HTTPException(409, "Termina la sesión antes de eliminarla.")
        try:
            runtime.store.delete(session_id)
        except OSError:
            raise HTTPException(500, "No se pudieron eliminar los archivos de audio. La sesión permanece en el historial para reintentar.")
        await runtime.release(session_id)
        return Response(status_code=204)

    @app.get("/api/sessions/{session_id}/audio")
    async def audio(session_id: str, original: bool = False):
        session = runtime.session(session_id)
        directory = runtime.store.session_dir(session_id)
        recording_name = Path(session["config"].get("recording_artifact", "recording.wav")).name
        paths = [directory / recording_name, directory / "normalized.wav", directory / "caller.wav"]
        if original and session["source"] == "file":
            name = Path(session["config"].get("original_artifact", "original.wav")).name
            paths = [directory / name]
        for path in paths:
            if path.is_file():
                media_type = {".wav": "audio/wav", ".webm": "audio/webm", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}.get(path.suffix)
                # The caller WAV and the final stereo recording share this route.
                # Never reuse a response obtained while capture was still active.
                return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})
        raise HTTPException(404, "No hay audio guardado para esta sesión.")

    @app.get("/api/sessions/{session_id}/export")
    async def export(session_id: str, format: Literal["json", "csv"] = "json"):
        session = runtime.session(session_id)
        if format == "json":
            return Response(json.dumps(session, ensure_ascii=False, indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="session-{session_id}.json"'})
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        labels = ["angry", "disgusted", "fearful", "happy", "neutral", "other", "sad", "surprised", "unknown"]
        writer.writerow(["session_id", "start_ms", "end_ms", "status", "label", "latency_ms", "voiced_ms", *labels, "config_json"])
        for window in session["emotions"]:
            writer.writerow([session_id, window["start_ms"], window["end_ms"], window["status"], window["label"],
                             window["latency_ms"], window["voiced_ms"], *(window["scores"].get(label, "") for label in labels),
                             json.dumps(session["config"], ensure_ascii=False)])
        return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="emotions-{session_id}.csv"'})

    @app.websocket("/api/sessions/{session_id}/stream")
    async def stream_socket(websocket: WebSocket, session_id: str):
        if websocket.headers.get("origin") not in config.allowed_origins:
            await websocket.close(code=1008, reason="Origen no permitido")
            return
        session = runtime.store.get(session_id)
        if not session or session["source"] != "live" or session["status"] not in {"created", "connecting", "live"}:
            await websocket.close(code=1008, reason="La sesión no está activa")
            return
        if runtime.listeners.get(session_id):
            await websocket.close(code=1008, reason="La sesión ya tiene una conexión de audio")
            return
        await websocket.accept()
        runtime.listeners.setdefault(session_id, set()).add(websocket)
        await runtime.send(websocket, {"type": "session", "session": session})
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    current_stream = runtime.streams.get(session_id)
                    if not current_stream:
                        raise ValueError("Envía audio_config antes de audio PCM.")
                    await current_stream.push(message["bytes"])
                    continue
                if len(message.get("text", "")) > 65_536:
                    raise ValueError("El evento supera el tamaño permitido.")
                event = json.loads(message.get("text", "{}"))
                event_type = event.get("type")
                if event_type == "audio_config":
                    sample_rate = event.get("sample_rate")
                    if not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 96_000:
                        raise ValueError("Frecuencia de muestreo inválida")
                    if session_id in runtime.streams:
                        raise ValueError("La frecuencia de audio ya está configurada.")
                    runtime.streams[session_id] = LiveStream(runtime, session_id, sample_rate)
                    runtime.store.mutate(session_id, lambda s: s.update(audio_url=f"/api/sessions/{session_id}/audio"))
                    runtime.store.mutate(session_id, lambda s: s["config"].update(microphone_sample_rate=sample_rate))
                elif event_type == "transcript":
                    fragment = TranscriptFragment.model_validate(event.get("fragment", {})).model_dump()
                    def add(session):
                        if not any(previous["id"] == fragment["id"] for previous in session["transcript"]):
                            session["transcript"].append(fragment)
                    runtime.store.mutate(session_id, add)
                elif event_type == "drain":
                    event_id = event.get("event_id")
                    if not isinstance(event_id, str) or str(UUID(event_id)) != event_id:
                        raise ValueError("Identificador de confirmación inválido")
                    # WebSocket messages are ordered. At this point every earlier
                    # PCM packet and transcript fragment has been persisted. The
                    # browser can safely issue its separate HTTP finish request.
                    await runtime.send(websocket, {"type": "drained", "event_id": event_id})
                elif event_type == "started":
                    if runtime.session(session_id)["status"] not in {"created", "connecting", "live"}:
                        continue
                    runtime.store.mutate(session_id, lambda s: s.update(status="live", stage="Llamada en curso"))
                    offset = event.get("recording_offset_ms", 0)
                    if isinstance(offset, (int, float)) and 0 <= offset <= 60_000:
                        runtime.store.mutate(session_id, lambda s: s["config"].update(recording_offset_ms=offset))
                    capture = event.get("capture_settings")
                    if isinstance(capture, dict):
                        allowed = {key: value for key, value in capture.items()
                                   if key in {"sampleRate", "channelCount", "echoCancellation", "noiseSuppression", "autoGainControl"}
                                   and isinstance(value, (str, bool, int, float))}
                        runtime.store.mutate(session_id, lambda s: s["config"].update(capture_settings=allowed))
                    await runtime.changed(session_id)
                elif event_type == "warning":
                    await runtime.warning(session_id, str(event.get("message", ""))[:500])
                else:
                    raise ValueError("Evento de audio desconocido")
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        except (ValueError, TypeError):
            await runtime.send(websocket, {"type": "error", "message": "No se pudo recibir el audio: revisa el formato o el límite de duración."})
            await websocket.close(code=1008)
        finally:
            runtime.listeners.get(session_id, set()).discard(websocket)
            runtime.send_locks.pop(websocket, None)
            current = runtime.store.get(session_id)
            if current and current["status"] in {"created", "connecting", "live"}:
                runtime.store.mutate(session_id, lambda s: s.update(status="processing", stage="Conexión interrumpida; conservando resultados"))
                await runtime.warning(session_id, "Se interrumpió la conexión de análisis. La sesión conserva resultados parciales.")
                runtime.launch_finish(session_id, False)

    return app


app = create_app()
