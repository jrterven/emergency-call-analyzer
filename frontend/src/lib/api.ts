import type { ResearchSession } from '../types'

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, headers: { ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers } })
  if (!response.ok) {
    let detail: unknown
    try { detail = (await response.json()).detail } catch { detail = response.statusText }
    throw new Error(typeof detail === 'string' ? detail : `La solicitud falló (${response.status}).`)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}

export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'Ocurrió un error inesperado.' }
export function getSession(id: string): Promise<ResearchSession> { return apiRequest(`/api/sessions/${encodeURIComponent(id)}`) }
