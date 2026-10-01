import type { ResearchSession } from '../types'

/** HTTP snapshots and WebSocket events can arrive out of order. Results are
 * append-only within one session, so a delayed snapshot must not erase them. */
export function mergeSession(previous: ResearchSession | null, incoming: ResearchSession): ResearchSession {
  if (!previous || previous.id !== incoming.id) return incoming
  const incomingIsNewer = incoming.updated_at >= previous.updated_at
  const latest = incomingIsNewer ? incoming : previous
  const earlier = incomingIsNewer ? previous : incoming

  function mergeResults<T extends { id: string }>(older: T[], newer: T[]): T[] {
    const byId = new Map(older.map(item => [item.id, item]))
    newer.forEach(item => byId.set(item.id, item))
    // The fullest list retains the provider's original fragment order. Pending
    // local fragments are appended without changing their text or timestamps.
    const backbone = newer.length >= older.length ? newer : older
    const seen = new Set(backbone.map(item => item.id))
    return [...backbone, ...[...older, ...newer].filter(item => {
      if (seen.has(item.id)) return false
      seen.add(item.id)
      return true
    })].map(item => byId.get(item.id)!)
  }

  return {
    ...latest,
    duration_ms: Math.max(previous.duration_ms, incoming.duration_ms),
    transcript: mergeResults(earlier.transcript, latest.transcript),
    emotions: mergeResults(earlier.emotions, latest.emotions)
      .sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms),
    summary: latest.summary ?? earlier.summary,
    audio_url: latest.audio_url ?? earlier.audio_url,
    original_audio_url: latest.original_audio_url ?? earlier.original_audio_url,
    warnings: [...new Set([...previous.warnings, ...incoming.warnings])],
    config: { ...earlier.config, ...latest.config },
  }
}
