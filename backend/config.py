"""Environment configuration. Model loading never happens here."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(ROOT / "data"))).expanduser().resolve())
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", "").strip())
    live_model: str = field(default_factory=lambda: os.getenv("LIVE_MODEL", "gpt-live-1"))
    summary_model: str = field(default_factory=lambda: os.getenv("SUMMARY_MODEL", "gpt-6-luna"))
    whisper_model: str = field(default_factory=lambda: os.getenv("WHISPER_MODEL", "small"))
    emotion_model: str = field(default_factory=lambda: os.getenv("EMOTION_MODEL", "emotion2vec/emotion2vec_plus_base"))
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:8000", "http://127.0.0.1:8000",
    )
    max_upload_bytes: int = 25 * 1024 * 1024
    max_recording_bytes: int = 180 * 1024 * 1024
    max_file_duration_ms: int = 600_000
    max_live_duration_ms: int = 900_000
    window_ms: int = 4_000
    hop_ms: int = 2_000


settings = Settings()
