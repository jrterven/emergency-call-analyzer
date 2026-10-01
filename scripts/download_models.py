"""Download and load the two local models before running the first experiment."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from backend.inference import _get_emotion, _get_whisper, get_model_metadata

print("Cargando emotion2vec+ en CPU…", flush=True)
_get_emotion()
print("Cargando Whisper local en CPU…", flush=True)
_get_whisper()
print("Modelos listos:", get_model_metadata())
