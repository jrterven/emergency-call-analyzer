import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Activity, ArrowDownToLine, ArrowLeft, ArrowUpRight, AudioLines, Check, CheckCircle2, ChevronRight, Circle, Clock3, FileAudio, Headphones, History, Info, LayoutDashboard, LoaderCircle, MapPin, Mic, MicOff, Phone, PhoneOff, Play, Plus, Radio, Settings2, ShieldCheck, Square, Trash2, Upload, Users, Waves, X } from 'lucide-react'
import { EMOTION_COLORS, EMOTION_LABELS } from './types'
import type { EmotionWindow, Health, ResearchSession, TranscriptFragment } from './types'
import { apiRequest, errorMessage, getSession } from './lib/api'
import { LiveCall } from './lib/live'
import { mergeSession } from './lib/sessionState'

type Tab = 'live' | 'file' | 'history'
const ongoing = (s: ResearchSession) => ['created', 'connecting', 'live', 'processing'].includes(s.status)
const statusLabels: Record<string, string> = { created: 'Preparada', connecting: 'Conectando', live: 'En curso', processing: 'Procesando', completed: 'Completada', partial: 'Resultados parciales', failed: 'Interrumpida' }
const stageLabels: Record<string, string> = { created: 'Lista para iniciar', connecting: 'Conectando con el asistente', live: 'Escuchando y analizando', uploaded: 'Grabación recibida', queued: 'Esperando procesamiento', decoding: 'Preparando el audio', emotions: 'Analizando emociones', emotion: 'Analizando emociones', transcribing: 'Transcribiendo con Whisper', transcription: 'Transcribiendo con Whisper', summarizing: 'Preparando el resumen', summary: 'Preparando el resumen', recording: 'Guardando grabación', finishing: 'Finalizando la sesión', completed: 'Análisis completado', partial: 'Análisis con resultados parciales', failed: 'Procesamiento interrumpido' }
const time = (ms: number) => { const seconds = Math.max(0, Math.floor(ms / 1000)); return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}` }
const date = (value: string) => new Intl.DateTimeFormat('es-MX', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value))

function Badge({ children, tone = 'muted' }: { children: ReactNode; tone?: string }) { return <span className={`badge badge-${tone}`}>{children}</span> }
function PanelTitle({ icon, title, aside }: { icon: ReactNode; title: string; aside?: ReactNode }) { return <div className="panel-title"><div>{icon}<h2>{title}</h2></div>{aside}</div> }

function EmotionChart({ session }: { session: ResearchSession | null }) {
  const windows = session?.emotions ?? []
  const valid = windows.filter(w => w.status === 'ok')
  const duration = Math.max(session?.duration_ms ?? 0, ...windows.map(w => w.end_ms), 10000)
  const chartWidth = 760, chartHeight = 168, left = 38, top = 12, right = 744, bottom = 132
  const x = (ms: number) => left + (ms / duration) * (right - left)
  const y = (value: number) => bottom - Math.min(1, Math.max(0, value)) * (bottom - top)
  return <section className="panel timeline-panel">
    <PanelTitle icon={<Activity size={17} />} title="Señales emocionales" aside={<span className="micro-copy">Ventanas de 4 s · cada 2 s</span>} />
    <div className="chart-wrap">
      <svg viewBox={`0 0 ${chartWidth} ${chartHeight}`} role="img" aria-label={valid.length ? 'Puntuaciones de las nueve emociones a lo largo del audio' : 'Gráfica sin datos emocionales'}>
        {[0, .25, .5, .75, 1].map(v => <g key={v}><line x1={left} x2={right} y1={y(v)} y2={y(v)} stroke="#deded6" strokeDasharray="3 5" /><text x={left - 10} y={y(v) + 4} textAnchor="end" fill="#737774" fontSize="10">{v.toFixed(v % 1 ? 2 : 0)}</text></g>)}
        {[0, .25, .5, .75, 1].map(v => <text key={v} x={x(duration * v)} y={156} textAnchor={v === 0 ? 'start' : v === 1 ? 'end' : 'middle'} fill="#737774" fontSize="10">{time(duration * v)}</text>)}
        {Object.keys(EMOTION_LABELS).map(key => { let connected = false; const d = windows.map(w => { if (w.status !== 'ok' || w.scores[key] == null) { connected = false; return '' } const point = `${connected ? 'L' : 'M'} ${x((w.start_ms + w.end_ms) / 2)} ${y(w.scores[key])}`; connected = true; return point }).join(' '); return <g key={key}><path d={d} fill="none" stroke={EMOTION_COLORS[key]} strokeWidth="2" strokeLinecap="round" />{valid.map(w => w.scores[key] == null ? null : <circle key={w.id} cx={x((w.start_ms + w.end_ms) / 2)} cy={y(w.scores[key])} r={valid.length === 1 ? 3 : 1.5} fill={EMOTION_COLORS[key]} />)}</g> })}
      </svg>
      {!valid.length && <div className="chart-empty"><Waves size={25} /><span>Las emociones aparecerán aquí</span><small>{windows.length ? 'Aún no hay ventanas con suficiente voz.' : 'Inicia una llamada o analiza una grabación.'}</small></div>}
    </div>
    <div className="chart-legend">{Object.entries(EMOTION_LABELS).map(([key, label]) => <span key={key}><i style={{ background: EMOTION_COLORS[key] }} />{label}</span>)}</div>
    <div className="panel-footnote"><Info size={13} />Estimaciones del modelo.{windows.some(w => w.status === 'skipped' || w.status === 'error') && <span className="text-warning"> Hay intervalos sin resultados.</span>}</div>
  </section>
}

function EmotionScores({ session }: { session: ResearchSession | null }) {
  const last = [...(session?.emotions ?? [])].reverse().find(w => w.status === 'ok')
  const newest: EmotionWindow | undefined = session?.emotions.at(-1)
  return <section className="panel emotion-panel">
    <PanelTitle icon={<Waves size={17} />} title="Emoción estimada" aside={<Badge tone={last ? 'teal' : 'muted'}>{last ? time(last.end_ms) : 'Sin datos'}</Badge>} />
    <div className="emotion-headline"><h3>{last?.label ? EMOTION_LABELS[last.label] ?? last.label : 'Esperando audio'}</h3><p>{last ? `${time(last.start_ms)} – ${time(last.end_ms)} · ${Math.round(last.voiced_ms / 1000)} s de voz` : 'Voz del llamante'}</p></div>
    <div className="score-list">{Object.entries(EMOTION_LABELS).map(([key, label]) => <div className="score-row" key={key}><div><i style={{ background: EMOTION_COLORS[key] }} /><span>{label}</span></div><div className="score-track"><div style={{ width: `${(last?.scores[key] ?? 0) * 100}%`, background: EMOTION_COLORS[key] }} /></div><span className="score-value">{last ? (last.scores[key] ?? 0).toFixed(2) : '—'}</span></div>)}</div>
    <div className="emotion-note"><Circle size={10} />{newest?.status === 'insufficient_audio' ? 'Audio insuficiente en la ventana más reciente' : newest?.status === 'skipped' ? 'Ventana reciente omitida por retraso del modelo' : newest?.status === 'error' ? 'No se pudo analizar la ventana reciente' : 'emotion2vec+ · procesamiento local'}</div>
  </section>
}

function Transcript({ session, source, seek }: { session: ResearchSession | null; source: 'live' | 'file'; seek: (ms: number) => void }) {
  const viewport = useRef<HTMLDivElement>(null)
  const followLatest = useRef(true)
  const transcript = session?.source === 'live' ? session.transcript.reduce<TranscriptFragment[]>((groups, fragment) => {
    const previous = groups.at(-1)
    if (previous && previous.speaker === fragment.speaker && (previous.end_ms == null || fragment.start_ms == null || fragment.start_ms - previous.end_ms <= 1500)) {
      previous.text += fragment.text
      previous.end_ms = fragment.end_ms ?? previous.end_ms
    } else groups.push({ ...fragment })
    return groups
  }, []) : session?.transcript ?? []
  useEffect(() => { followLatest.current = true }, [session?.id])
  useEffect(() => {
    const element = viewport.current
    if (element && session?.status === 'live' && followLatest.current) element.scrollTop = element.scrollHeight
  }, [session?.transcript, session?.status])
  return <section className="panel transcript-panel">
    <PanelTitle icon={<AudioLines size={17} />} title="Transcripción" aside={<Badge>{source === 'file' ? 'Whisper local' : 'GPT Live'}</Badge>} />
    <div className="transcript-scroll" ref={viewport} tabIndex={0} aria-label="Transcripción de la sesión" onScroll={() => {
      const element = viewport.current
      if (element) followLatest.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48
    }}>
      {transcript.length ? transcript.map(t => <div key={t.id} className={`transcript-item transcript-${t.speaker}`}>
        <span className="speaker-avatar">{t.speaker === 'caller' ? <Mic size={14} /> : <Headphones size={14} />}</span>
        <div><div className="transcript-label"><strong>{t.speaker === 'caller' ? 'Llamante' : 'Asistente virtual'}</strong>{t.start_ms != null && <button className="timestamp" onClick={() => seek(t.start_ms!)} aria-label={`Reproducir desde ${time(t.start_ms)}`}>{time(t.start_ms)}{source === 'file' && <Play size={10} />}</button>}</div><p>{t.text}</p></div>
      </div>) : <p className="transcript-placeholder">Esperando transcripción…</p>}
    </div>
  </section>
}

function Summary({ session, busy, canSummarize, onRetry }: { session: ResearchSession | null; busy: boolean; canSummarize: boolean; onRetry: () => void }) {
  const summary = session?.summary
  return <section className="panel summary-panel"><PanelTitle icon={<LayoutDashboard size={17} />} title="Resumen del incidente" />
    {summary ? <div className="summary-content"><div className="summary-incident"><span className="eyebrow">SITUACIÓN DESCRITA</span><p>{summary.incident ?? 'No se identificó una descripción del incidente.'}</p></div><div className="summary-field"><MapPin size={15} /><div><span>Ubicación</span><p>{summary.location ?? 'Desconocida'}</p></div></div><div className="summary-field"><Users size={15} /><div><span>Personas afectadas</span><p>{summary.people_affected ?? 'Desconocidas'}</p></div></div><div className="summary-risks"><span className="eyebrow">RIESGOS MENCIONADOS</span>{summary.immediate_risks.length ? <ul>{summary.immediate_risks.map((risk, i) => <li key={i}>{risk}</li>)}</ul> : <p>No se mencionaron riesgos específicos.</p>}</div>{!!summary.missing_information.length && <div className="missing-info"><Info size={15} /><div><strong>Información pendiente</strong><ul>{summary.missing_information.map((item, i) => <li key={i}>{item}</li>)}</ul></div></div>}</div> : <div className="empty-block"><span className="empty-icon"><LayoutDashboard size={25} /></span><p>Resumen disponible al terminar.</p>{session && !ongoing(session) && <button className="button button-secondary" disabled={!canSummarize || busy || !session.transcript.length} onClick={onRetry}>{busy ? <LoaderCircle size={15} className="spin" /> : <Plus size={15} />}Generar resumen</button>}</div>}
  </section>
}

export default function App() {
  const [tab, setTab] = useState<Tab>('live')
  const [health, setHealth] = useState<Health | null>(null)
  const [sessions, setSessions] = useState<ResearchSession[]>([])
  const [session, setSession] = useState<ResearchSession | null>(null)
  const [backendError, setBackendError] = useState('')
  const [notice, setNotice] = useState('')
  const [settings, setSettings] = useState(false)
  const [liveStatus, setLiveStatus] = useState('Lista para iniciar')
  const [level, setLevel] = useState(0)
  const [muted, setMuted] = useState(false)
  const [liveBusy, setLiveBusy] = useState(false)
  const [callRunning, setCallRunning] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [file, setFile] = useState<File | null>(null)
  const [fileBuffer, setFileBuffer] = useState<AudioBuffer | null>(null)
  const [channel, setChannel] = useState(0)
  const [fileLoading, setFileLoading] = useState(false)
  const [fileError, setFileError] = useState('')
  const [uploadBusy, setUploadBusy] = useState(false)
  const [previewPlaying, setPreviewPlaying] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [summaryBusy, setSummaryBusy] = useState(false)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const call = useRef<LiveCall | null>(null)
  const startTime = useRef(0)
  const audio = useRef<HTMLAudioElement>(null)
  const preview = useRef<AudioBufferSourceNode | null>(null)
  const context = useRef<AudioContext | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const fileSelection = useRef(0)
  const selectionVersion = useRef(0)
  const latestCall = useRef<ResearchSession | null>(null)
  const sessionId = session?.id
  const activeId = health?.active_session_id
  const locked = !!activeId || liveBusy || callRunning || uploadBusy

  async function refresh() {
    const results = await Promise.allSettled([apiRequest<Health>('/api/health'), apiRequest<ResearchSession[]>('/api/sessions')])
    if (results[0].status === 'fulfilled') { setHealth(results[0].value); setBackendError('') } else setBackendError('El servidor local no está disponible. Inicia el backend en el puerto 8000.')
    if (results[1].status === 'fulfilled') setSessions(results[1].value)
  }
  useEffect(() => { void refresh(); const timer = window.setInterval(() => { void refresh() }, 3000); return () => window.clearInterval(timer) }, [])
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    const poll = async () => {
      const version = selectionVersion.current
      try { const result = await getSession(sessionId); if (!cancelled && version === selectionVersion.current) updateSession(result) }
      catch (error) { if (!cancelled && version === selectionVersion.current) setNotice(errorMessage(error)) }
    }
    const timer = window.setInterval(() => { void poll() }, 1000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [sessionId])
  useEffect(() => { if (!callRunning) return; const timer = window.setInterval(() => setElapsed(Date.now() - startTime.current), 500); return () => window.clearInterval(timer) }, [callRunning])
  useEffect(() => () => { call.current?.dispose(); preview.current?.stop(); void context.current?.close() }, [])
  useEffect(() => {
    if (!settings && !deleteConfirm) return
    const previousFocus = document.activeElement as HTMLElement | null
    const modal = document.querySelector<HTMLElement>('.modal')
    const selector = 'button:not(:disabled), a[href], input:not(:disabled), [tabindex="0"]'
    modal?.querySelector<HTMLElement>(selector)?.focus()
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setSettings(false); if (!deleteBusy) setDeleteConfirm(false) }
      if (event.key === 'Tab' && modal) {
        const elements = [...modal.querySelectorAll<HTMLElement>(selector)]
        const first = elements[0], last = elements.at(-1)
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener('keydown', handler)
    return () => { document.removeEventListener('keydown', handler); previousFocus?.focus() }
  }, [settings, deleteConfirm, deleteBusy])

  function selectSession(next: ResearchSession | null) {
    selectionVersion.current++
    setSession(next)
  }
  function updateSession(incoming: ResearchSession) {
    setSession(previous => previous?.id === incoming.id ? mergeSession(previous, incoming) : previous)
  }
  async function startCall() {
    setNotice(''); setLiveBusy(true); setElapsed(0); setMuted(false)
    selectSession(null)
    latestCall.current = null
    let firstSnapshot = true
    const current: LiveCall = new LiveCall({
      onSession: incoming => {
        if (call.current !== current) return
        const merged = mergeSession(latestCall.current, incoming)
        latestCall.current = merged
        if (firstSnapshot) { firstSnapshot = false; selectSession(merged) }
        else updateSession(merged)
        if (['processing', 'completed', 'partial', 'failed'].includes(merged.status)) { setCallRunning(false); setLevel(0) }
      },
      onStatus: status => { if (call.current === current) setLiveStatus(status) },
      onLevel: value => { if (call.current === current) setLevel(value) },
      onError: message => { if (call.current === current) setNotice(message) },
      onMuted: value => { if (call.current === current) setMuted(value) },
    })
    call.current = current
    try {
      const created = await current.start()
      if (call.current !== current) return
      latestCall.current = mergeSession(latestCall.current, created)
      updateSession(latestCall.current)
      startTime.current = Date.now(); setCallRunning(true); void refresh()
    } catch (error) {
      if (call.current === current) { setNotice(errorMessage(error)); current.dispose(); call.current = null; void refresh() }
    } finally { if (call.current === current || !call.current) setLiveBusy(false) }
  }
  async function stopCall() { setLiveBusy(true); try { await call.current?.stop() } catch (error) { setNotice(errorMessage(error)) } finally { setCallRunning(false); setMuted(false); setLevel(0); setLiveBusy(false); void refresh() } }
  function changeTab(next: Tab) {
    if ((callRunning || liveBusy) && next === 'file') return
    setTab(next); setNotice(''); setDeleteConfirm(false)
    if (next === 'live' && latestCall.current && (callRunning || liveBusy || activeId === latestCall.current.id)) selectSession(latestCall.current)
    else if (!callRunning && !liveBusy && !activeId) selectSession(null)
  }
  async function openSession(id: string) {
    if ((callRunning || liveBusy) && id !== session?.id) { setNotice('Termina la llamada en curso para abrir otra sesión.'); return }
    setNotice(''); setDeleteConfirm(false)
    const version = ++selectionVersion.current
    try { const result = await getSession(id); if (version === selectionVersion.current) { setSession(result); setTab('history') } }
    catch (error) { if (version === selectionVersion.current) setNotice(errorMessage(error)) }
  }
  function stopPreview() { preview.current?.stop(); preview.current = null; setPreviewPlaying(false) }
  async function selectFile(selected: File | undefined) {
    if (!selected) return
    stopPreview(); const selection = ++fileSelection.current; setFile(null); setFileBuffer(null); setFileError(''); setChannel(0)
    if (!/\.(wav|mp3|m4a|webm)$/i.test(selected.name)) { setFileError('Selecciona un archivo WAV, MP3, M4A o WebM.'); return }
    if (selected.size > 25 * 1024 * 1024) { setFileError('El archivo supera el límite de 25 MB.'); return }
    setFileLoading(true)
    try { if (!context.current || context.current.state === 'closed') context.current = new AudioContext(); const decoded = await context.current.decodeAudioData(await selected.arrayBuffer()); if (selection !== fileSelection.current) return; if (decoded.duration > 600) throw new Error('La grabación supera el límite de 10 minutos.'); if (decoded.numberOfChannels > 2) throw new Error('Este prototipo admite grabaciones mono o estéreo.'); setFile(selected); setFileBuffer(decoded) } catch (error) { if (selection === fileSelection.current) setFileError(error instanceof DOMException ? 'No se pudo previsualizar este audio. Verifica el archivo o conviértelo a WAV o MP3.' : errorMessage(error)) } finally { if (selection === fileSelection.current) setFileLoading(false) }
  }
  async function playPreview() { if (previewPlaying) { stopPreview(); return } if (!fileBuffer) return; try { if (!context.current || context.current.state === 'closed') context.current = new AudioContext(); await context.current.resume(); const isolated = context.current.createBuffer(1, fileBuffer.length, fileBuffer.sampleRate); isolated.copyToChannel(fileBuffer.getChannelData(channel), 0); const source = context.current.createBufferSource(); source.buffer = isolated; source.connect(context.current.destination); source.onended = () => { setPreviewPlaying(false); preview.current = null }; source.start(); preview.current = source; setPreviewPlaying(true) } catch (error) { setFileError(errorMessage(error)) } }
  async function uploadFile() {
    if (!file) return
    stopPreview(); setUploadBusy(true); setNotice('')
    let created: ResearchSession | null = null
    try { created = await apiRequest<ResearchSession>('/api/sessions', { method: 'POST', body: JSON.stringify({ source: 'file', title: file.name }) }); selectSession(created); const data = new FormData(); data.append('file', file); data.append('channel', String(channel)); const result = await apiRequest<ResearchSession>(`/api/sessions/${created.id}/upload`, { method: 'POST', body: data }); updateSession(result); void refresh() } catch (error) { setNotice(errorMessage(error)); if (created) { const latest = await getSession(created.id).catch(() => created); if (latest?.status === 'created') { await apiRequest(`/api/sessions/${created.id}`, { method: 'DELETE' }).catch(() => undefined); selectSession(null); } else if (latest) updateSession(latest) } void refresh() } finally { setUploadBusy(false) }
  }
  async function retrySummary() { if (!session) return; setSummaryBusy(true); try { const result = await apiRequest<ResearchSession>(`/api/sessions/${session.id}/summary`, { method: 'POST' }); updateSession(result) } catch (error) { setNotice(errorMessage(error)) } finally { setSummaryBusy(false) } }
  async function deleteSession() { if (!session) return; setDeleteBusy(true); try { await apiRequest(`/api/sessions/${session.id}`, { method: 'DELETE' }); selectSession(null); setDeleteConfirm(false); void refresh() } catch (error) { setNotice(errorMessage(error)) } finally { setDeleteBusy(false) } }
  function seek(ms: number) { if (audio.current && session?.audio_url) { const offset = session.source === 'live' && typeof session.config.recording_offset_ms === 'number' ? session.config.recording_offset_ms : 0; audio.current.currentTime = (ms + offset) / 1000; void audio.current.play().catch(error => setNotice(errorMessage(error))) } }
  const displayLive = tab === 'live' || (session?.source === 'live' && tab === 'history')
  const displayFile = tab === 'file' || (session?.source === 'file' && tab === 'history')
  const title = tab === 'history' ? 'Historial' : tab === 'file' ? 'Analizar archivo' : 'Llamada en vivo'

  return <div className="app-shell">
    <aside className="sidebar"><a href="#" className="brand" onClick={e => { e.preventDefault(); changeTab('live') }}><span className="brand-mark"><AudioLines size={25} /></span><span>Emergency Analyzer</span></a><nav aria-label="Navegación principal"><button className={tab === 'live' ? 'nav-item active' : 'nav-item'} onClick={() => changeTab('live')}><Phone size={18} />Llamada en vivo{callRunning && <i className="live-dot" />}</button><button className={tab === 'file' ? 'nav-item active' : 'nav-item'} disabled={callRunning || liveBusy} onClick={() => changeTab('file')}><FileAudio size={18} />Analizar archivo</button><button className={tab === 'history' ? 'nav-item active' : 'nav-item'} onClick={() => changeTab('history')}><History size={18} />Historial<span className="nav-count">{sessions.length}</span></button></nav><div className="sidebar-bottom"><button className="nav-item" onClick={() => setSettings(true)}><Settings2 size={18} />Configuración<ArrowUpRight size={14} /></button><div className="local-storage"><span><i className={backendError ? 'offline' : ''} />{backendError ? 'Servidor desconectado' : health ? 'Entorno local conectado' : 'Conectando al servidor'}</span></div></div></aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb"><span className="mobile-brand">Emergency Analyzer</span><span className="desktop-breadcrumb">{tab === 'live' ? 'Llamada en vivo' : tab === 'file' ? 'Archivos de audio' : 'Historial'}</span></div><div className="topbar-right"><button className="icon-button" onClick={() => setSettings(true)} aria-label="Abrir configuración"><Settings2 size={18} /></button></div></header>
    <main><div className="page-heading"><h1>{title}</h1></div>
      {backendError && <div className="alert alert-error" role="alert"><Info size={18} /><span>{backendError}</span><button onClick={() => void refresh()} className="text-button">Reintentar</button></div>}
      {notice && <div className="alert alert-error" role="alert"><Info size={18} /><span>{notice}</span><button className="icon-button" aria-label="Cerrar mensaje" onClick={() => setNotice('')}><X size={16} /></button></div>}
      {callRunning && tab !== 'live' && <div className="alert"><Phone size={17} /><span>La llamada sigue en curso · {time(elapsed)}</span><button className="text-button" onClick={() => changeTab('live')}>Volver a la llamada</button><button className="button button-small button-danger" disabled={liveBusy} onClick={() => void stopCall()}><PhoneOff size={14} />Terminar</button></div>}
      {activeId && !callRunning && session?.id !== activeId && <div className="alert"><LoaderCircle size={17} className="spin" /><span>Hay una sesión activa. Puedes consultar el historial mientras termina.</span><button className="text-button" onClick={() => void openSession(activeId)}>Ver sesión</button></div>}
      {session?.warnings.map((warning, index) => <div className="alert alert-warning" key={index}><Info size={17} /><span>{warning}</span></div>)}
      {tab === 'history' && !session ? <section className="panel history-panel"><PanelTitle icon={<History size={18} />} title="Sesiones guardadas" aside={<Badge>{sessions.length} sesiones</Badge>} />{sessions.length ? <div className="history-table"><div className="history-table-header"><span>SESIÓN</span><span>ESTADO</span><span>DURACIÓN</span><span>FECHA</span><span /></div>{sessions.map(s => <button className="history-row" key={s.id} onClick={() => void openSession(s.id)}><span className="history-name"><span className="history-icon">{s.source === 'live' ? <Phone size={17} /> : <FileAudio size={17} />}</span><span><strong>{s.title}</strong><small>{s.source === 'live' ? 'Llamada simulada' : 'Grabación de audio'}</small></span></span><Badge tone={s.status === 'completed' ? 'teal' : ['failed', 'partial'].includes(s.status) ? 'amber' : ongoing(s) ? 'blue' : 'muted'}>{statusLabels[s.status]}</Badge><span className="tabular">{time(s.duration_ms)}</span><span>{date(s.created_at)}</span><ChevronRight size={17} /></button>)}</div> : <div className="history-empty empty-block"><span className="empty-icon"><History size={29} /></span><h3>No hay sesiones guardadas</h3><button className="button button-primary" disabled={locked} onClick={() => changeTab('file')}><Upload size={16} />Analizar primera grabación</button></div>}</section> : <>
      {tab === 'history' && session && <div className="session-toolbar"><button className="text-button" onClick={() => { selectSession(null); setDeleteConfirm(false) }}><ArrowLeft size={15} />Todas las sesiones</button><span className="session-id">SESIÓN {session.id.slice(0, 8).toUpperCase()}</span><div><a className="button button-small button-secondary" href={`/api/sessions/${session.id}/export?format=json`} download><ArrowDownToLine size={14} />JSON</a><a className="button button-small button-secondary" href={`/api/sessions/${session.id}/export?format=csv`} download><ArrowDownToLine size={14} />CSV</a><button className="icon-button danger" disabled={ongoing(session)} onClick={() => setDeleteConfirm(true)} aria-label="Eliminar sesión"><Trash2 size={16} /></button></div></div>}
      <div className="analysis-grid">
        <div className="primary-column">
          {displayLive && <section className="panel call-panel"><PanelTitle icon={<Radio size={17} />} title={tab === 'history' ? 'Grabación de la llamada' : 'Llamada simulada'} aside={<Badge tone={callRunning ? 'teal' : session ? 'muted' : 'blue'}><i className={callRunning ? 'live-dot' : 'badge-dot'} />{callRunning ? 'En vivo' : session ? statusLabels[session.status] : 'WebRTC · GPT Live'}</Badge>} /><div className="call-center"><div className={`call-emblem ${callRunning ? 'is-live' : ''}`}><div><Phone size={28} /></div><i /><i /></div><h3>{callRunning ? 'Llamada en curso' : liveBusy ? 'Conectando la llamada' : session && tab === 'history' ? session.title : 'Listo para iniciar'}</h3><p>{callRunning || liveBusy ? liveStatus : session && tab === 'history' ? date(session.created_at) : 'Simulación con asistente de voz'}</p>{callRunning && <div className="call-wave" aria-label={`Nivel del micrófono: ${Math.round(level * 100)} por ciento`}>{Array.from({ length: 37 }, (_, i) => <i key={i} style={{ height: `${callRunning && !muted ? 5 + level * (12 + 31 * Math.abs(Math.sin(i * 2.7))) : 4 + 8 * Math.abs(Math.sin(i * 2.7))}px`, opacity: callRunning && !muted ? .65 + level * .35 : .2 + .3 * Math.abs(Math.sin(i)) }} />)}</div>}<div className="call-time"><Clock3 size={14} /><span>{time(callRunning ? elapsed : session?.duration_ms ?? elapsed)}</span><span className="call-time-label">{muted ? 'Micrófono silenciado' : 'Duración'}</span></div>
            {tab !== 'history' && <div className="call-actions">{callRunning ? <><button className={`button button-secondary ${muted ? 'is-muted' : ''}`} onClick={() => { const next = !muted; call.current?.mute(next); setMuted(next) }} disabled={liveBusy}>{muted ? <MicOff size={17} /> : <Mic size={17} />}{muted ? 'Activar micrófono' : 'Silenciar'}</button><button className="button button-danger" disabled={liveBusy} onClick={() => void stopCall()}>{liveBusy ? <LoaderCircle className="spin" size={17} /> : <PhoneOff size={17} />}Terminar llamada</button></> : <button className="button button-primary call-start" disabled={locked || !health?.openai_configured || !!backendError} onClick={() => void startCall()}>{liveBusy ? <LoaderCircle size={17} className="spin" /> : <Phone size={17} />}{liveBusy ? 'Conectando…' : 'Iniciar llamada'}</button>}</div>}
          </div>{!health?.openai_configured && tab !== 'history' && <div className="configuration-note"><Info size={15} /><span>Configura <code>OPENAI_API_KEY</code> en el servidor para iniciar llamadas.</span><button className="text-button" onClick={() => setSettings(true)}>Ver configuración<ArrowUpRight size={12} /></button></div>}</section>}
          {displayFile && <section className="panel upload-panel"><PanelTitle icon={<FileAudio size={17} />} title={session ? 'Grabación de audio' : 'Selecciona una grabación'} aside={<Badge tone="teal">Whisper local</Badge>} />{!session ? <div className="upload-content"><input ref={fileInput} type="file" accept=".wav,.mp3,.m4a,.webm,audio/wav,audio/mpeg,audio/mp4,audio/webm" hidden onChange={e => void selectFile(e.target.files?.[0])} /><div role="button" tabIndex={locked ? -1 : 0} aria-disabled={locked} className={`drop-zone ${dragging ? 'dragging' : ''} ${file ? 'has-file' : ''}`} onClick={() => { if (!locked) fileInput.current?.click() }} onKeyDown={e => { if (!locked && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); fileInput.current?.click() } }} onDragOver={e => { e.preventDefault(); if (!locked) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); if (!locked) void selectFile(e.dataTransfer.files[0]) }}><span className="upload-icon">{fileLoading ? <LoaderCircle size={27} className="spin" /> : file ? <Check size={27} /> : <Upload size={27} />}</span><h3>{fileLoading ? 'Preparando vista previa…' : file ? file.name : 'Arrastra un archivo de audio'}</h3><p>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB · ${time((fileBuffer?.duration ?? 0) * 1000)} · ${fileBuffer?.numberOfChannels === 2 ? 'Estéreo' : 'Mono'}` : 'o explora los archivos de tu computadora'}</p><span className="file-formats">WAV · MP3 · M4A · WEBM<span />25 MB máx. · 10 minutos</span></div>{fileError && <div role="alert" className="file-error"><Info size={15} />{fileError}</div>}{file && <div className="channel-selector"><div><span className="eyebrow">CANAL DEL LLAMANTE</span><p>{fileBuffer?.numberOfChannels === 2 ? 'Escucha y elige la voz que quieres analizar.' : 'Usa una grabación con una sola voz aislada.'}</p></div><div className="channel-buttons">{[0, ...(fileBuffer?.numberOfChannels === 2 ? [1] : [])].map(c => <button key={c} className={`channel-button ${channel === c ? 'selected' : ''}`} onClick={() => { stopPreview(); setChannel(c) }}>{channel === c && <Check size={13} />}{fileBuffer?.numberOfChannels === 2 ? c === 0 ? 'Izquierdo' : 'Derecho' : 'Mono'}</button>)}<button className="icon-button preview-button" onClick={() => void playPreview()} aria-label={previewPlaying ? 'Detener vista previa' : 'Escuchar canal seleccionado'}>{previewPlaying ? <Square size={15} /> : <Play size={15} />}</button></div></div>}<div className="upload-bottom"><span><ShieldCheck size={14} />Transcripción y emociones locales</span><button className="button button-primary" disabled={!file || locked || !health?.ffmpeg_available || !!backendError} onClick={() => void uploadFile()}>{uploadBusy ? <LoaderCircle size={16} className="spin" /> : <Activity size={16} />}Analizar grabación<ArrowUpRight size={14} /></button></div>{health && !health.ffmpeg_available && <div className="file-error"><Info size={15} />FFmpeg no está disponible. Consulta la configuración.</div>}</div> : <div className="file-session"><div className="file-session-icon"><FileAudio size={27} /></div><div><h3>{session.filename ?? session.title}</h3><p>Canal {session.channel === 0 ? 'izquierdo / mono' : 'derecho'} · {time(session.duration_ms)}</p><Badge tone={ongoing(session) ? 'blue' : session.status === 'completed' ? 'teal' : 'amber'}>{ongoing(session) && <LoaderCircle size={12} className="spin" />}{statusLabels[session.status]}</Badge></div></div>}</section>}
          {session && ongoing(session) && session.status !== 'live' && <div className="processing-banner" aria-live="polite"><LoaderCircle size={19} className="spin" /><div><strong>{stageLabels[session.stage] ?? session.stage}</strong></div><span className="processing-dots"><i /><i /><i /></span></div>}
          {session && !ongoing(session) && ['failed', 'partial'].includes(session.status) && <div className="alert alert-warning"><Info size={17} /><span>Esta sesión tiene resultados parciales. El audio y los resultados disponibles se conservan para revisión.</span></div>}
          {session?.audio_url && <section className="panel playback-panel"><div><Headphones size={17} /><strong>Escuchar grabación</strong><span>{session.source === 'live' ? 'Llamante: izquierda · Asistente: derecha' : 'Canal seleccionado del llamante'}</span></div><audio ref={audio} key={session.id} controls src={session.audio_url} preload="metadata" />{session.source === 'file' && session.original_audio_url && <a href={session.original_audio_url} download className="text-button">Audio original<ArrowDownToLine size={13} /></a>}</section>}
          <EmotionChart session={session} />
        </div>
        <EmotionScores session={session} />
      </div>
      <div className="details-grid"><Transcript session={session} source={displayFile ? 'file' : 'live'} seek={seek} /><Summary session={session} busy={summaryBusy} canSummarize={!!health?.openai_configured} onRetry={() => void retrySummary()} /></div>
      {tab !== 'history' && session && !ongoing(session) && <div className="session-complete"><CheckCircle2 size={17} /><span>Sesión guardada en el historial.</span><button className="text-button" onClick={() => void openSession(session.id)}>Abrir sesión<ArrowUpRight size={14} /></button>{displayFile && <button className="button button-small button-secondary" disabled={locked} onClick={() => { selectSession(null); setFile(null); setFileBuffer(null) }}><Plus size={14} />Otra grabación</button>}</div>}
      </>}

    </main></div>
    {settings && <div className="modal-backdrop" onClick={() => setSettings(false)}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="settings-title" onClick={e => e.stopPropagation()}><div className="modal-heading"><span className="modal-icon"><Settings2 size={23} /></span><div><span className="eyebrow">ENTORNO LOCAL</span><h2 id="settings-title">Configuración</h2></div><button className="icon-button" aria-label="Cerrar configuración" onClick={() => setSettings(false)}><X size={19} /></button></div><p className="modal-description">Las credenciales viven en el servidor. Los archivos se transcriben localmente, sin una API de transcripción.</p><div className="settings-row"><div><Radio size={17} /><span>GPT Live y resúmenes<small>Conexión a OpenAI</small></span></div><Badge tone={health?.openai_configured ? 'teal' : 'amber'}>{health?.openai_configured ? 'Configurado' : 'Sin API key'}</Badge></div><div className="settings-instructions">Añade <code>OPENAI_API_KEY=tu_clave</code> al archivo <code>.env</code> del proyecto y reinicia el backend. La clave nunca se envía al navegador.</div><div className="settings-row"><div><AudioLines size={17} /><span>Whisper local<small>Modelo {health?.whisper_model ?? 'small'} · CPU</small></span></div><Badge>{health?.models_loaded.whisper ? 'Cargado' : 'Carga bajo demanda'}</Badge></div><div className="settings-row"><div><Activity size={17} /><span>emotion2vec+<small>Modelo {health?.emotion_model?.split('/').at(-1) ?? 'emotion2vec_plus_base'}</small></span></div><Badge>{health?.models_loaded.emotion ? 'Cargado' : 'Carga bajo demanda'}</Badge></div><div className="settings-row"><div><FileAudio size={17} /><span>FFmpeg<small>Decodificación de audio</small></span></div><Badge tone={health?.ffmpeg_available ? 'teal' : 'amber'}>{health?.ffmpeg_available ? 'Disponible' : 'No disponible'}</Badge></div>{health && !health.ffmpeg_available && <div className="settings-instructions">Instala FFmpeg para tu sistema siguiendo el README y reinicia el backend.</div>}<div className="settings-note"><Info size={16} /><span>La primera ejecución descarga los pesos de los modelos locales. Una vez descargados, Whisper y emotion2vec+ trabajan en esta computadora.</span></div><button className="button button-primary modal-close" onClick={() => setSettings(false)}>Entendido<Check size={16} /></button></section></div>}
    {deleteConfirm && session && <div className="modal-backdrop"><section className="modal delete-modal" role="dialog" aria-modal="true" aria-labelledby="delete-title"><span className="modal-icon danger"><Trash2 size={24} /></span><h2 id="delete-title">Eliminar esta sesión</h2><p>Se eliminarán “{session.title}”, su audio, transcripción y resultados de esta computadora.</p><div className="delete-actions"><button className="button button-secondary" disabled={deleteBusy} onClick={() => setDeleteConfirm(false)}>Cancelar</button><button className="button button-danger" disabled={deleteBusy} onClick={() => void deleteSession()}>{deleteBusy ? <LoaderCircle size={16} className="spin" /> : <Trash2 size={16} />}Eliminar sesión</button></div></section></div>}
  </div>
}
