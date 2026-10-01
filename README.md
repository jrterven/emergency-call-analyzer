# Línea Clara

Prototipo de investigación para observar conversaciones simuladas de recepción de emergencias: audio, transcripción, emociones y resumen. Interfaz en español de México. Las puntuaciones emocionales apoyan la revisión humana; no clasifican la veracidad de una llamada.

## Ejecutar en esta Mac

Requisitos: Python 3.11, Node.js 22 o posterior, Chrome y FFmpeg (`brew install ffmpeg`).

```bash
./scripts/setup.sh
# Opcional: añade OPENAI_API_KEY a .env para llamadas y resúmenes.
./scripts/dev.sh
```

Abre [http://127.0.0.1:5173](http://127.0.0.1:5173) en Chrome. La interfaz también muestra el estado de configuración. Una API key heredada del entorno tiene prioridad sobre `.env`; las credenciales nunca se envían al navegador.

Para descargar los pesos antes de la primera sesión:

```bash
.venv/bin/python scripts/download_models.py
```

Los pesos de Whisper `small` y emotion2vec+ ocupan aproximadamente 1.6 GB, además de las dependencias. Se guardan en `.model-cache/` y se reutilizan sin conexión. El primer análisis puede tardar más por las descargas y la inicialización.

## Dos modos

- **Llamada en vivo:** WebRTC conecta el micrófono con `gpt-live-1`. GPT-Live proporciona las transcripciones de ambos participantes. Un AudioWorklet independiente envía únicamente el micrófono al análisis emocional local. La grabación estéreo conserva al llamante a la izquierda y al asistente a la derecha.
- **Archivos:** WAV, MP3, M4A o WebM, hasta 25 MB y 10 minutos. Primero escucha y selecciona el canal del llamante; FFmpeg extrae ese canal antes de convertirlo a mono de 16 kHz. La misma señal, con silencios intactos, alimenta emotion2vec+ y Whisper local. Las conversaciones mezcladas en un solo canal requieren preparar previamente una voz aislada.

Los trabajos locales se ejecutan de uno en uno, fuera del servidor HTTP. En archivos se calculan las emociones antes de transcribir. En vivo se usan ventanas de 4 segundos cada 2 segundos; una ventana necesita al menos un segundo de voz. Si el modelo se retrasa, se registra la ventana omitida y se prioriza la reciente. El audio completo del llamante se conserva independientemente de esas omisiones.

Whisper es el paquete local `openai-whisper`, modelo multilingüe `small`, CPU y FP32. No hay fallback a una API de transcripción. Sus segmentos tienen tiempos estimados. Los fragmentos de GPT-Live mantienen sus tiempos originales; el reloj de la grabación y el de la sesión tienen un desplazamiento aproximado registrado en la configuración.

## Coste y datos

La transcripción y el reconocimiento emocional de **archivos** permanecen en esta Mac. El resumen usa `gpt-6-luna` y envía solamente texto. Las llamadas en vivo envían audio a OpenAI y usan un backend delegado; ambas capacidades consumen API. Los archivos se pueden analizar sin API key, dejando el resumen pendiente.

El historial se guarda en `data/sessions.sqlite3`, y cada sesión tiene su directorio de audio. Se conserva hasta eliminarlo desde la interfaz. JSON exporta resultados, prompts, parámetros, modelos y revisiones; CSV exporta las ventanas emocionales. Al reiniciar se marcan como parciales las sesiones inconclusas. Si falla una etapa, se conservan los datos ya disponibles.

La app está preparada para localhost y una sesión activa a la vez. `scripts/dev.sh` enlaza ambos servidores a `127.0.0.1`; el backend valida los orígenes de HTTP/WebSocket. No es un despliegue público ni se conecta a telefonía real.

## Configuración

Consulta `.env.example`. Opciones principales:

| Variable | Valor inicial |
| --- | --- |
| `WHISPER_MODEL` | `small` |
| `EMOTION_MODEL` | `emotion2vec/emotion2vec_plus_base` |
| `EMOTION_MODEL_REVISION` | `main`; el hash resuelto se exporta |
| `LIVE_MODEL` | `gpt-live-1` |
| `SUMMARY_MODEL` | `gpt-6-luna` |
| `TORCH_NUM_THREADS` | `4` |
| `MODEL_CACHE_DIR` | `.model-cache` |
| `DATA_DIR` | `data` |

La carga del modelo es diferida; la pantalla de configuración indica cuándo está cargado. `requirements-lock.txt` registra las versiones verificadas en macOS ARM64 con Python 3.11. Las licencias de código y pesos son independientes; emotion2vec+ conserva la referencia a la licencia de modelos de FunASR en su ficha oficial.

## Verificación

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend run build
```

Las pruebas usan directorios temporales y verifican canales, frecuencias de muestreo, silencio, persistencia, cierres parciales y límites. No requieren llamadas facturadas ni descargar modelos. Las pruebas con modelos reales se realizan por separado usando audio sintético en español.

La calidad en llamadas reales, acentos, ruido y voces superpuestas requiere evaluación con anotaciones humanas. Los resultados de este prototipo son estimaciones para investigación.

## API local

- `GET /api/health`, `GET /api/sessions`, `GET /api/sessions/{id}`.
- `POST /api/sessions` con `source: live | file`; reserva el único trabajo activo.
- `POST /api/sessions/{id}/connect` con oferta SDP para GPT-Live.
- `POST /api/sessions/{id}/upload` con archivo y canal; procesamiento asíncrono.
- `POST /api/sessions/{id}/recording` y `/finish` para guardar/cerrar llamadas.
- `POST /api/sessions/{id}/summary` para reintentar un resumen.
- `GET /api/sessions/{id}/audio`, `/export?format=json|csv`, `DELETE /api/sessions/{id}`.
- `WS /api/sessions/{id}/stream`: configuración de frecuencia, PCM16 mono, fragmentos de transcripción y actualizaciones emocionales.

Swagger está disponible en [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

Fuentes: [GPT-Live](https://developers.openai.com/api/docs/guides/live), [Whisper](https://github.com/openai/whisper), [emotion2vec+ base](https://huggingface.co/emotion2vec/emotion2vec_plus_base).
