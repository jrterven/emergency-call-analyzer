"""Small SQLite persistence layer with atomic document mutations."""

import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "sessions.sqlite3"
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, document TEXT NOT NULL)")
        for session in self.list():
            if session["status"] in {"created", "connecting", "live", "processing"}:
                self.mutate(session["id"], lambda s: s.update(
                    status="partial", stage="Interrumpida al reiniciar",
                    warnings=[*s["warnings"], "La aplicación se reinició. Se conservaron los resultados parciales."],
                ))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def session_dir(self, session_id: str) -> Path:
        # Only server-generated UUIDs can ever select artifact directories.
        if str(UUID(session_id)) != session_id:
            raise ValueError("Identificador de sesión inválido")
        return self.data_dir / session_id

    def create(self, source: str, title: str | None, config: dict) -> dict:
        session_id = str(uuid4())
        stamp = now_iso()
        session = {
            "id": session_id, "source": source,
            "title": title.strip() if title and title.strip() else ("Llamada simulada" if source == "live" else "Grabación de investigación"),
            "status": "created", "stage": "Lista para conectar" if source == "live" else "Esperando archivo",
            "created_at": stamp, "updated_at": stamp, "duration_ms": 0,
            "channel": 0, "filename": None, "audio_url": None,
            "original_audio_url": None, "transcript": [], "emotions": [],
            "summary": None, "warnings": [], "config": config,
        }
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (session_id, stamp, json.dumps(session, ensure_ascii=False)))
        self.session_dir(session_id).mkdir(parents=True)
        return session

    def get(self, session_id: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT document FROM sessions WHERE id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self) -> list[dict]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT document FROM sessions ORDER BY created_at DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def mutate(self, session_id: str, change: Callable[[dict], None]) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT document FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            session = json.loads(row[0])
            change(session)
            session["updated_at"] = now_iso()
            db.execute("UPDATE sessions SET document=? WHERE id=?", (json.dumps(session, ensure_ascii=False, allow_nan=False), session_id))
        return session

    def delete(self, session_id: str) -> None:
        artifact_dir = self.session_dir(session_id)
        # Remove audio first. A filesystem failure must not make it disappear from
        # the history while leaving its sensitive audio behind without a retry path.
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
