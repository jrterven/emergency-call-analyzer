export type SessionSource = 'live' | 'file'
export type SessionStatus = 'created' | 'connecting' | 'live' | 'processing' | 'completed' | 'partial' | 'failed'

export interface TranscriptFragment {
  id: string
  speaker: 'caller' | 'assistant'
  text: string
  start_ms: number | null
  end_ms: number | null
}

export interface EmotionWindow {
  id: string
  start_ms: number
  end_ms: number
  scores: Record<string, number>
  label: string | null
  status: 'ok' | 'insufficient_audio' | 'skipped' | 'error'
  latency_ms: number
  voiced_ms: number
  error?: string
}

export interface IncidentSummary {
  incident: string | null
  location: string | null
  people_affected: string | null
  immediate_risks: string[]
  missing_information: string[]
  note: string | null
}

export interface ResearchSession {
  id: string
  source: SessionSource
  title: string
  status: SessionStatus
  stage: string
  created_at: string
  updated_at: string
  duration_ms: number
  channel: number
  filename: string | null
  audio_url: string | null
  original_audio_url: string | null
  transcript: TranscriptFragment[]
  emotions: EmotionWindow[]
  summary: IncidentSummary | null
  warnings: string[]
  config: Record<string, unknown>
}

export interface Health {
  openai_configured: boolean
  ffmpeg_available: boolean
  whisper_model: string
  emotion_model: string
  active_session_id: string | null
  models_loaded: { whisper: boolean; emotion: boolean }
}

export type StreamEvent =
  | { type: 'session'; session: ResearchSession }
  | { type: 'emotion'; window: EmotionWindow }
  | { type: 'warning'; message: string }
  | { type: 'error'; message: string }
  | { type: 'drained'; event_id: string }

export const EMOTION_LABELS: Record<string, string> = {
  angry: 'Enfado', disgusted: 'Asco', fearful: 'Miedo', happy: 'Alegría',
  neutral: 'Neutral', other: 'Otra', sad: 'Tristeza', surprised: 'Sorpresa', unknown: 'Desconocida',
}

export const EMOTION_COLORS: Record<string, string> = {
  angry: '#fb7185', disgusted: '#a3e635', fearful: '#c084fc', happy: '#fbbf24',
  neutral: '#5eead4', other: '#94a3b8', sad: '#60a5fa', surprised: '#fb923c', unknown: '#64748b',
}
