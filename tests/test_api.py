"""API, artifact and concurrency checks; no provider calls or model downloads."""

import asyncio
import io
import json
import shutil
import subprocess
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import LiveStream, Runtime, create_app
from backend.storage import Store

ORIGIN = {"origin": "http://localhost:5173"}


def silent_wav(channels=1, duration=2, sample_rate=48000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\0" * (channels * sample_rate * duration * 2))
    return output.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", openai_api_key=""))) as test_client:
        yield test_client


def wait_finished(client, session_id):
    for _ in range(200):
        session = client.get(f"/api/sessions/{session_id}").json()
        if session["status"] != "processing":
            return session
        time.sleep(0.01)
    pytest.fail("The local task did not finish")


def create_session(client, source):
    response = client.post("/api/sessions", json={"source": source})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_origin_and_single_active_session(client):
    assert client.post("/api/sessions", json={"source": "file"}, headers={"Origin": "https://external.example"}).status_code == 403
    session_id = create_session(client, "file")
    assert client.post("/api/sessions", json={"source": "live"}).status_code == 409
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get("/api/health").json()["active_session_id"] is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_local_file_results_export_and_deletion(client):
    session_id = create_session(client, "file")
    response = client.post(f"/api/sessions/{session_id}/upload", files={"file": ("silence.wav", silent_wav(), "audio/wav")}, data={"channel": "0"})
    assert response.status_code == 202
    session = wait_finished(client, session_id)
    assert session["status"] == "completed"
    assert session["transcript"] == []
    assert session["emotions"][0]["status"] == "insufficient_audio"
    assert session["duration_ms"] == 2000
    assert client.get(f"/api/sessions/{session_id}/audio").status_code == 200
    export = client.get(f"/api/sessions/{session_id}/export").json()
    assert export["config"]["prompt_version"]
    assert "openai_api_key" not in json.dumps(export).lower()
    csv = client.get(f"/api/sessions/{session_id}/export?format=csv").text
    assert "latency_ms" in csv and "insufficient_audio" in csv
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert not client.app.state.runtime.store.session_dir(session_id).exists()
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_live_tail_transcript_dedup_and_stereo_webm(client, tmp_path):
    recording = tmp_path / "stereo.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2", "-c:a", "libopus", "-live", "1", str(recording)], check=True)
    session_id = create_session(client, "live")
    with client.websocket_connect(f"/api/sessions/{session_id}/stream", headers=ORIGIN) as socket:
        assert socket.receive_json()["type"] == "session"
        socket.send_json({"type": "audio_config", "sample_rate": 44100})
        socket.send_json({"type": "started", "recording_offset_ms": 120, "capture_settings": {"echoCancellation": True, "deviceId": "private"}})
        fragment = {"id": "one", "speaker": "caller", "text": "Hay un incendio", "start_ms": 120, "end_ms": 1600}
        socket.send_json({"type": "transcript", "fragment": fragment})
        socket.send_json({"type": "transcript", "fragment": fragment})
        for _ in range(10):
            socket.send_bytes(b"\0" * (44100 // 4 * 2))
        receipt_id = str(uuid4())
        socket.send_json({"type": "drain", "event_id": receipt_id})
        for _ in range(20):
            receipt = socket.receive_json()
            if receipt["type"] == "drained":
                assert receipt["event_id"] == receipt_id
                break
            assert receipt["type"] != "error", receipt
        else:
            pytest.fail("The stream did not acknowledge the queued audio")
        initial_playback = client.get(f"/api/sessions/{session_id}/audio")
        assert initial_playback.headers["content-type"] == "audio/wav"
        assert initial_playback.headers["cache-control"] == "no-store"
        response = client.post(f"/api/sessions/{session_id}/recording", files={"file": ("llamada.webm", recording.read_bytes(), "audio/webm")})
        assert response.status_code == 200, response.text
        playback = client.get(f"/api/sessions/{session_id}/audio")
        assert playback.headers["content-type"] == "audio/webm"
        assert playback.headers["cache-control"] == "no-store"
        assert playback.content != initial_playback.content
        from backend.audio import decode_audio, probe_audio
        playable = tmp_path / "playback.webm"
        playable.write_bytes(playback.content)
        assert probe_audio(playable)["channels"] == 2
        assert probe_audio(playable)["duration_ms"] == pytest.approx(2000, abs=40)
        for channel in (0, 1):
            np.testing.assert_array_equal(decode_audio(playable, channel), decode_audio(recording, channel))
        assert client.post(f"/api/sessions/{session_id}/finish", json={"complete": True, "duration_ms": 2500}).status_code == 202
    session = wait_finished(client, session_id)
    assert session["status"] == "completed"
    assert session["transcript"] == [fragment]
    assert session["emotions"][0]["start_ms"] == 0
    assert session["emotions"][0]["end_ms"] == 2500
    assert session["config"]["recording_offset_ms"] == 120
    assert session["config"]["capture_settings"] == {"echoCancellation": True}
    assert session["warnings"]  # No API key: local results remain usable.


def test_restart_preserves_completed_and_marks_unfinished_partial(tmp_path):
    store = Store(tmp_path)
    store.initialize()
    unfinished = store.create("live", None, {})
    completed = store.create("file", None, {})
    store.mutate(completed["id"], lambda session: session.update(status="completed"))
    store.initialize()
    assert store.get(unfinished["id"])["status"] == "partial"
    assert store.get(completed["id"])["status"] == "completed"


def test_drain_acknowledges_all_prior_pcm_and_transcript_before_finish(client):
    session_id = create_session(client, "live")
    fragment = {"id": "last-fragment", "speaker": "caller", "text": "Hay dos personas", "start_ms": 10, "end_ms": 900}
    event_id = str(uuid4())
    with client.websocket_connect(f"/api/sessions/{session_id}/stream", headers=ORIGIN) as socket:
        socket.receive_json()
        socket.send_json({"type": "audio_config", "sample_rate": 16000})
        for _ in range(4):
            socket.send_bytes(b"\0" * (4000 * 2))
        socket.send_json({"type": "transcript", "fragment": fragment})
        socket.send_json({"type": "drain", "event_id": event_id})
        assert socket.receive_json() == {"type": "drained", "event_id": event_id}
        assert client.app.state.runtime.streams[session_id].received == 16000
        assert client.get(f"/api/sessions/{session_id}").json()["transcript"] == [fragment]
        with wave.open(str(client.app.state.runtime.store.session_dir(session_id) / "caller.wav"), "rb") as caller:
            assert caller.getnframes() == 16000
        assert client.post(f"/api/sessions/{session_id}/finish", json={"complete": True, "duration_ms": 1000}).status_code == 202
    session = wait_finished(client, session_id)
    assert session["emotions"][0]["end_ms"] == 1000
    assert session["transcript"] == [fragment]


def test_failed_audio_deletion_keeps_history_for_retry(client, monkeypatch):
    session_id = create_session(client, "file")
    def denied(*args, **kwargs):
        raise PermissionError("Cannot remove fixture")
    monkeypatch.setattr("backend.storage.shutil.rmtree", denied)
    assert client.delete(f"/api/sessions/{session_id}").status_code == 500
    assert client.get(f"/api/sessions/{session_id}").status_code == 200
    assert client.get("/api/health").json()["active_session_id"] == session_id


def test_backlog_keeps_latest_window_and_complete_caller_audio(tmp_path, monkeypatch):
    def emotion(samples, start_ms, end_ms):
        return {"id": f"{start_ms}", "start_ms": start_ms, "end_ms": end_ms, "scores": {}, "label": None,
                "status": "insufficient_audio", "latency_ms": 0, "voiced_ms": 0}
    monkeypatch.setattr("backend.inference.analyze_emotion", emotion)
    async def scenario():
        runtime = Runtime(Settings(data_dir=tmp_path, openai_api_key=""))
        runtime.store.initialize()
        session_id = runtime.store.create("live", None, {})["id"]
        stream = LiveStream(runtime, session_id, 48000)
        # No event-loop yield until completion intentionally outruns the worker.
        for _ in range(12):
            await stream.push(b"\0" * (48000 * 2))
        await stream.finish()
        windows = runtime.store.get(session_id)["emotions"]
        assert len(windows) == 5
        assert sum(window["status"] == "skipped" for window in windows) == 4
        assert windows[-1]["status"] == "insufficient_audio"
        assert windows[-1]["end_ms"] == 12000
        with wave.open(str(tmp_path / session_id / "caller.wav"), "rb") as caller:
            assert caller.getnframes() == 12 * 48000
        runtime.executor.shutdown(wait=True)
    asyncio.run(scenario())


def test_provider_connection_failure_holds_slot_until_worker_cleanup(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def blocked_emotion(samples, start_ms, end_ms):
        entered.set()
        release.wait(timeout=3)
        return {"id": "blocked", "start_ms": start_ms, "end_ms": end_ms, "scores": {}, "label": None,
                "status": "insufficient_audio", "latency_ms": 0, "voiced_ms": 0}
    class FailedProvider:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(403, request=httpx.Request("POST", url), json={"error": "Provider private body"})
    monkeypatch.setattr("backend.inference.analyze_emotion", blocked_emotion)
    monkeypatch.setattr("backend.main.httpx.AsyncClient", FailedProvider)
    app = create_app(Settings(data_dir=tmp_path, openai_api_key="test-key"))
    with TestClient(app) as client:
        session_id = create_session(client, "live")
        with client.websocket_connect(f"/api/sessions/{session_id}/stream", headers=ORIGIN) as socket:
            socket.receive_json()
            socket.send_json({"type": "audio_config", "sample_rate": 16000})
            for _ in range(4):
                socket.send_bytes(b"\0" * (16000 * 2))
            assert entered.wait(timeout=2)
            failed = client.post(f"/api/sessions/{session_id}/connect", json={"sdp": "v=0\r\n"})
            assert failed.status_code == 502
            assert "Provider private body" not in failed.text
            assert client.post("/api/sessions", json={"source": "file"}).status_code == 409
            release.set()
        for _ in range(200):
            if client.get("/api/health").json()["active_session_id"] is None:
                break
            time.sleep(.01)
        assert client.get("/api/health").json()["active_session_id"] is None
        assert session_id not in app.state.runtime.streams


@pytest.mark.parametrize("provider_status", [200, 403])
def test_cancel_during_connect_does_not_overwrite_history_or_new_slot(tmp_path, monkeypatch, provider_status):
    entered, proceed = threading.Event(), threading.Event()
    sideband_calls, sideband_messages = [], []
    class DelayedProvider:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            entered.set()
            while not proceed.is_set():
                await asyncio.sleep(.01)
            return httpx.Response(provider_status, request=httpx.Request("POST", url), json={
                "session": {"id": "late-provider-session"}, "transport": {"type": "webrtc", "sdp": "v=0\r\n"},
            })
    monkeypatch.setattr("backend.main.httpx.AsyncClient", DelayedProvider)
    class Sideband:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def send(self, data): sideband_messages.append(json.loads(data))
        def __aiter__(self): return self
        async def __anext__(self): return json.dumps({"type": "session.closed"})
    def open_sideband(url, **kwargs):
        sideband_calls.append((url, kwargs))
        return Sideband()
    monkeypatch.setattr("backend.main.connect_sideband", open_sideband)
    with TestClient(create_app(Settings(data_dir=tmp_path, openai_api_key="test-key"))) as client, ThreadPoolExecutor(max_workers=1) as requests:
        session_id = create_session(client, "live")
        connecting = requests.submit(client.post, f"/api/sessions/{session_id}/connect", json={"sdp": "v=0\r\n"})
        try:
            assert entered.wait(timeout=2)
            assert client.post(f"/api/sessions/{session_id}/finish", json={"complete": False, "duration_ms": 0}).status_code == 202
            saved = wait_finished(client, session_id)
            assert saved["status"] == "partial"
            new_session_id = create_session(client, "file")
        finally:
            proceed.set()
        assert connecting.result(timeout=2).status_code == 409
        assert client.get(f"/api/sessions/{session_id}").json() == saved
        assert client.get("/api/health").json()["active_session_id"] == new_session_id
        assert client.delete(f"/api/sessions/{new_session_id}").status_code == 204
        if provider_status == 200:
            assert len(sideband_calls) == 1
            assert sideband_calls[0][0] == "wss://api.openai.com/v1/live/sessions/late-provider-session/attach"
            assert sideband_calls[0][1]["additional_headers"] == {"Authorization": "Bearer test-key"}
            assert sideband_messages[0]["type"] == "session.close"
        else:
            assert sideband_calls == []


def test_late_provider_cleanup_failure_is_sanitized_and_preserves_state(tmp_path, monkeypatch, caplog):
    def denied(*args, **kwargs):
        raise RuntimeError("Bearer private-key provider private-body")
    monkeypatch.setattr("backend.main.connect_sideband", denied)
    async def scenario():
        runtime = Runtime(Settings(data_dir=tmp_path, openai_api_key="private-key"))
        runtime.store.initialize()
        session = runtime.store.create("live", None, {})
        await runtime.close_late_provider_session("late-provider-session")
        assert runtime.store.get(session["id"]) == session
        assert runtime.active_session_id is None
        runtime.executor.shutdown(wait=True)
    asyncio.run(scenario())
    assert "No se confirmó el cierre remoto" in caplog.text
    assert "private-key" not in caplog.text and "private-body" not in caplog.text
