import { apiRequest, getSession } from './api'
import { mergeSession } from './sessionState'
import type { ResearchSession, StreamEvent, TranscriptFragment } from '../types'

export interface LiveCallbacks {
  onSession: (session: ResearchSession) => void
  onStatus: (status: string) => void
  onLevel: (level: number) => void
  onError: (message: string) => void
  onMuted?: (muted: boolean) => void
}

interface ConnectionAnswer {
  session: { id: string }
  transport: { type: 'webrtc'; sdp: string }
}

const waitFor = (target: EventTarget, name: string, timeoutMs: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  const timer = window.setTimeout(() => { cleanup(); reject(new Error('Se agotó el tiempo de conexión.')) }, timeoutMs)
  const handler = () => { cleanup(); resolve() }
  const abort = () => { cleanup(); reject(new Error('La llamada se canceló.')) }
  const cleanup = () => { clearTimeout(timer); target.removeEventListener(name, handler); signal?.removeEventListener('abort', abort) }
  target.addEventListener(name, handler, { once: true })
  signal?.addEventListener('abort', abort, { once: true })
  if (signal?.aborted) abort()
})

/** One caller track is fanned out to WebRTC, PCM analysis and channel 0 recording.
 * The assistant track is connected only to playback and channel 1 recording. */
export class LiveCall {
  private session: ResearchSession | null = null
  private microphone?: MediaStream
  private peer?: RTCPeerConnection
  private events?: RTCDataChannel
  private socket?: WebSocket
  private context?: AudioContext
  private caller?: MediaStreamAudioSourceNode
  private assistant?: MediaStreamAudioSourceNode
  private assistantPlayback?: HTMLAudioElement
  private pcm?: AudioWorkletNode
  private silentSink?: GainNode
  private merger?: ChannelMergerNode
  private destination?: MediaStreamAudioDestinationNode
  private recorder?: MediaRecorder
  private chunks: Blob[] = []
  private recordingStarted = 0
  private stopping?: Promise<void>
  private providerClosed?: () => void
  private providerStarted?: () => void
  private providerError?: (error: Error) => void
  private finalized = false
  private intentionalCleanup = false
  private muted = false
  private usage: unknown
  private maxDurationTimer?: number
  private pcmFlushed?: () => void
  private cancelled = false
  private lifetime = new AbortController()
  private drainWaiter?: { id: string; resolve: () => void }

  constructor(private callbacks: LiveCallbacks) {}

  private assertStarting() {
    if (this.cancelled) throw new Error('La llamada se canceló.')
  }

  async start(): Promise<ResearchSession> {
    this.assertStarting()
    if (this.session || this.microphone) throw new Error('Ya hay una llamada en curso.')
    this.callbacks.onStatus('Solicitando micrófono')
    try {
      const microphone = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false },
      })
      if (this.cancelled) { microphone.getTracks().forEach((track) => track.stop()); this.assertStarting() }
      this.microphone = microphone
      this.context = new AudioContext()
      await this.context.resume()
      this.assertStarting()
      const created = await apiRequest<ResearchSession>('/api/sessions', {
        method: 'POST', body: JSON.stringify({ source: 'live' }),
      })
      if (this.cancelled) {
        await apiRequest(`/api/sessions/${created.id}`, { method: 'DELETE' })
        this.assertStarting()
      }
      this.session = created
      this.callbacks.onSession(this.session)
      this.callbacks.onStatus('Conectando análisis local')
      const url = new URL(`/api/sessions/${this.session.id}/stream`, window.location.href)
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
      this.socket = new WebSocket(url)
      this.socket.addEventListener('message', ({ data }) => {
        try {
          const event = JSON.parse(data) as StreamEvent
          if (event.type === 'session') {
            this.session = mergeSession(this.session, event.session)
            this.callbacks.onSession(this.session)
          } else if (event.type === 'emotion' && this.session) {
            const emotions = this.session.emotions.filter((window) => window.id !== event.window.id)
            this.session = { ...this.session, emotions: [...emotions, event.window].sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms) }
            this.callbacks.onSession(this.session)
          } else if (event.type === 'error') this.callbacks.onError(event.message)
          else if (event.type === 'warning') this.callbacks.onStatus(event.message)
          else if (event.type === 'drained' && event.event_id === this.drainWaiter?.id) this.drainWaiter.resolve()
        } catch { this.callbacks.onError('No se pudo leer una actualización del análisis.') }
      })
      this.socket.addEventListener('close', () => {
        if (!this.intentionalCleanup && !this.stopping) {
          this.callbacks.onError('Se perdió la conexión con el análisis local. La sesión quedará incompleta.')
          void this.stop(false)
        }
      })
      if (this.socket.readyState !== WebSocket.OPEN) await waitFor(this.socket, 'open', 10_000, this.lifetime.signal)
      this.assertStarting()
      this.socket.send(JSON.stringify({ type: 'audio_config', sample_rate: this.context.sampleRate }))
      await this.prepareAudioGraph()
      this.assertStarting()

      this.callbacks.onStatus('Conectando GPT-Live')
      this.peer = new RTCPeerConnection()
      this.peer.addEventListener('track', ({ track }) => this.attachAssistant(track))
      for (const track of this.microphone.getAudioTracks()) this.peer.addTrack(track, this.microphone)
      this.peer.addEventListener('connectionstatechange', () => {
        if (this.peer?.connectionState === 'failed' && !this.stopping) {
          this.callbacks.onError('Se interrumpió la conexión de voz. Se conservarán los resultados disponibles.')
          void this.stop(false)
        }
      })
      this.events = this.peer.createDataChannel('oai-events')
      this.events.addEventListener('message', ({ data }) => this.handleProviderEvent(data))
      this.events.addEventListener('close', () => {
        if (!this.intentionalCleanup && !this.stopping && !this.finalized) {
          this.callbacks.onError('GPT-Live cerró la conexión de voz.')
          void this.stop(false)
        }
      })
      const offer = await this.peer.createOffer()
      this.assertStarting()
      await this.peer.setLocalDescription(offer)
      this.assertStarting()
      if (this.peer.iceGatheringState !== 'complete') {
        await new Promise<void>((resolve, reject) => {
          const peer = this.peer!
          const timer = window.setTimeout(() => { cleanup(); reject(new Error('No fue posible establecer la conexión de audio.')) }, 10_000)
          const handler = () => { if (peer.iceGatheringState === 'complete') { cleanup(); resolve() } }
          const abort = () => { cleanup(); reject(new Error('La llamada se canceló.')) }
          const cleanup = () => { clearTimeout(timer); peer.removeEventListener('icegatheringstatechange', handler); this.lifetime.signal.removeEventListener('abort', abort) }
          peer.addEventListener('icegatheringstatechange', handler)
          this.lifetime.signal.addEventListener('abort', abort, { once: true })
          handler()
        })
      }
      this.assertStarting()
      const sdp = this.peer.localDescription?.sdp
      if (!sdp) throw new Error('No se pudo preparar la conexión WebRTC.')
      const answer = await apiRequest<ConnectionAnswer>(`/api/sessions/${this.session.id}/connect`, {
        method: 'POST', body: JSON.stringify({ sdp }),
      })
      this.assertStarting()
      // Install the start waiter BEFORE applying the answer; session.started may arrive immediately.
      const started = new Promise<void>((resolve, reject) => {
        const cleanup = () => { clearTimeout(timer); this.lifetime.signal.removeEventListener('abort', abort); this.providerError = undefined; this.providerStarted = undefined }
        const abort = () => { cleanup(); reject(new Error('La llamada se canceló.')) }
        const timer = window.setTimeout(() => { cleanup(); reject(new Error('GPT-Live no confirmó el inicio de la sesión.')) }, 20_000)
        this.providerStarted = () => { cleanup(); resolve() }
        this.providerError = (error) => { cleanup(); reject(error) }
        this.lifetime.signal.addEventListener('abort', abort, { once: true })
      })
      // A failed remote description can occur before awaiting the startup promise.
      void started.catch(() => undefined)
      await this.peer.setRemoteDescription({ type: 'answer', sdp: answer.transport.sdp })
      this.assertStarting()
      await started
      this.assertStarting()
      this.maxDurationTimer = window.setTimeout(() => { this.callbacks.onStatus('Límite de 10 minutos alcanzado'); void this.stop() }, 600_000)
      const result = await getSession(this.session.id)
      this.assertStarting()
      this.session = mergeSession(this.session, result)
      return this.session
    } catch (error) {
      const message = error instanceof Error ? error.message : 'No fue posible iniciar la llamada.'
      await this.stop(false)
      throw new Error(message)
    }
  }

  private async prepareAudioGraph() {
    const context = this.context!
    await context.audioWorklet.addModule('/pcm-worklet.js')
    this.assertStarting()
    this.caller = context.createMediaStreamSource(this.microphone!)
    this.pcm = new AudioWorkletNode(context, 'caller-pcm', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] })
    this.silentSink = context.createGain()
    this.silentSink.gain.value = 0
    this.caller.connect(this.pcm)
    this.pcm.connect(this.silentSink).connect(context.destination)
    this.pcm.port.onmessage = ({ data }) => {
      if (data.type === 'flushed') { this.pcmFlushed?.(); this.pcmFlushed = undefined; return }
      if (data.type !== 'pcm') return
      this.callbacks.onLevel(this.muted ? 0 : Math.min(1, data.level * 5))
      if (this.socket?.readyState === WebSocket.OPEN) {
        if (this.socket.bufferedAmount > 2 * context.sampleRate * 4) {
          this.callbacks.onError('El análisis local perdió el ritmo de captura. Se conservará la grabación parcial.')
          if (!this.stopping) void this.stop(false)
          return
        }
        this.socket.send(data.buffer)
      }
    }
    this.merger = context.createChannelMerger(2)
    this.destination = context.createMediaStreamDestination()
    this.destination.channelCount = 2
    this.destination.channelCountMode = 'explicit'
    this.caller.connect(this.merger, 0, 0)
    this.merger.connect(this.destination)
    const mime = ['audio/webm;codecs=opus', 'audio/webm'].find((type) => MediaRecorder.isTypeSupported(type))
    if (!mime) throw new Error('Este prototipo requiere Chrome con grabación WebM/Opus.')
    this.recorder = new MediaRecorder(this.destination.stream, { mimeType: mime, audioBitsPerSecond: 128_000 })
    this.recorder.addEventListener('dataavailable', ({ data }) => { if (data.size) this.chunks.push(data) })
    this.recorder.addEventListener('error', () => this.callbacks.onError('Se produjo un error al grabar el audio local.'))
    this.recordingStarted = performance.now()
    this.recorder.start(1000)
  }

  private attachAssistant(track: MediaStreamTrack) {
    if (!this.context || !this.merger || this.intentionalCleanup) return
    this.assistant?.disconnect()
    this.assistantPlayback?.pause()
    if (this.assistantPlayback) this.assistantPlayback.srcObject = null
    const stream = new MediaStream([track])
    // Chrome needs a playing media element to pull remote WebRTC audio.
    // That element handles playback; WebAudio feeds only the right recording channel.
    const playback = new Audio()
    playback.autoplay = true
    playback.srcObject = stream
    this.assistantPlayback = playback
    void playback.play().catch(() => {
      if (this.intentionalCleanup || this.stopping) return
      this.callbacks.onError('El navegador bloqueó la voz del asistente. Permite audio en este sitio y vuelve a iniciar la llamada.')
      void this.stop(false)
    })
    this.assistant = this.context.createMediaStreamSource(stream)
    this.assistant.connect(this.merger, 0, 1)
  }

  private handleProviderEvent(raw: string) {
    let event: Record<string, unknown>
    try { event = JSON.parse(raw) } catch { return }
    if (event.type === 'session.started') {
      const providerSession = event.session as { id?: string } | undefined
      this.sendLocal({ type: 'started', live_session_id: providerSession?.id,
        recording_offset_ms: Math.round(performance.now() - this.recordingStarted),
        capture_settings: this.microphone?.getAudioTracks()[0]?.getSettings(),
      })
      this.callbacks.onStatus('Llamada en curso')
      this.events?.send(JSON.stringify({ type: 'session.instructions.append', event_id: crypto.randomUUID(), delegation_id: null,
        content: 'Saluda ahora en español de México, con voz calmada y natural. Di: «Emergencias, ¿dónde ocurre la situación?». Luego haz una pausa para escuchar.',
      }))
      this.providerStarted?.()
      this.providerStarted = undefined
    } else if (event.type === 'session.input_transcript.delta' || event.type === 'session.output_transcript.delta') {
      const fragment: TranscriptFragment = {
        id: typeof event.event_id === 'string' ? event.event_id : crypto.randomUUID(),
        speaker: event.type === 'session.input_transcript.delta' ? 'caller' : 'assistant',
        text: typeof event.delta === 'string' ? event.delta : '',
        start_ms: typeof event.start_ms === 'number' ? event.start_ms : null,
        end_ms: typeof event.end_ms === 'number' ? event.end_ms : null,
      }
      if (fragment.text && this.session && !this.session.transcript.some(item => item.id === fragment.id)) {
        this.session = { ...this.session, transcript: [...this.session.transcript, fragment] }
        this.callbacks.onSession(this.session)
      }
      this.sendLocal({ type: 'transcript', fragment })
    } else if (event.type === 'session.closed') {
      this.finalized = true
      this.usage = event.usage
      this.providerClosed?.()
      if (!this.stopping) void this.stop(true)
    } else if (event.type === 'error') {
      const error = new Error('GPT-Live reportó un error. Revisa el acceso al modelo y la conexión.')
      this.callbacks.onError(error.message)
      this.providerError?.(error)
      this.sendLocal({ type: 'warning', message: error.message })
    }
  }

  private sendLocal(message: Record<string, unknown>) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(message))
  }

  mute(muted: boolean) {
    this.muted = muted
    // Track disabling silences GPT, PCM analysis and caller recording together.
    this.microphone?.getAudioTracks().forEach((track) => { track.enabled = !muted })
    this.callbacks.onMuted?.(muted)
    if (muted) this.callbacks.onLevel(0)
  }

  stop(complete = true): Promise<void> {
    if (this.stopping) return this.stopping
    this.cancelled = true
    this.lifetime.abort()
    this.stopping = this.finish(complete)
    return this.stopping
  }

  private async finish(complete: boolean) {
    try {
    clearTimeout(this.maxDurationTimer)
    if (this.session) this.callbacks.onStatus('Guardando sesión')
    let closed = this.finalized
    if (!closed && this.events?.readyState === 'open') {
      const events = this.events
      closed = await new Promise<boolean>((resolve) => {
        const timer = window.setTimeout(() => { this.providerClosed = undefined; resolve(false) }, 15_000)
        this.providerClosed = () => { clearTimeout(timer); resolve(true) }
        try { events.send(JSON.stringify({ type: 'session.close' })) }
        catch { clearTimeout(timer); this.providerClosed = undefined; resolve(false) }
      })
    }
    if (this.pcm) {
      const flushed = await new Promise<boolean>((resolve) => {
        const timer = window.setTimeout(() => { this.pcmFlushed = undefined; resolve(false) }, 1000)
        this.pcmFlushed = () => { clearTimeout(timer); resolve(true) }
        this.pcm!.port.postMessage('flush')
      })
      if (!flushed) { complete = false; this.callbacks.onError('No se confirmó el cierre de la captura de audio; la sesión se guardará como parcial.') }
    }
    if (this.socket?.readyState === WebSocket.OPEN) {
      const drained = await new Promise<boolean>((resolve) => {
        const id = crypto.randomUUID()
        const timer = window.setTimeout(() => { this.drainWaiter = undefined; resolve(false) }, 5000)
        this.drainWaiter = { id, resolve: () => { clearTimeout(timer); this.drainWaiter = undefined; resolve(true) } }
        this.sendLocal({ type: 'drain', event_id: id })
      })
      if (!drained) { complete = false; this.callbacks.onError('No se confirmó la recepción del último audio; la sesión se guardará como parcial.') }
    } else if (this.session) complete = false
    if (this.recorder && this.recorder.state !== 'inactive') {
      const recorder = this.recorder
      await new Promise<void>((resolve) => {
        recorder.addEventListener('stop', () => resolve(), { once: true })
        try { recorder.stop() } catch { complete = false; resolve() }
      })
    }
    const duration = this.recordingStarted ? Math.round(performance.now() - this.recordingStarted) : 0
    // Stop capturing immediately; saving and CPU inference may take longer.
    this.microphone?.getTracks().forEach((track) => track.stop())
    this.caller?.disconnect()
    this.assistant?.disconnect()
    this.assistantPlayback?.pause()
    if (this.assistantPlayback) this.assistantPlayback.srcObject = null
    if (this.session) {
      try {
        if (this.chunks.length) {
          const body = new FormData()
          body.append('file', new Blob(this.chunks, { type: 'audio/webm' }), 'llamada.webm')
          await apiRequest(`/api/sessions/${this.session.id}/recording`, { method: 'POST', body })
        }
      } catch {
        complete = false
        this.sendLocal({ type: 'warning', message: 'No se pudo guardar la grabación completa; el audio del llamante puede estar disponible.' })
        this.callbacks.onError('No se pudo guardar la grabación de ambos participantes.')
      }
      try {
        const result = await apiRequest<ResearchSession>(`/api/sessions/${this.session.id}/finish`, {
          method: 'POST', body: JSON.stringify({ complete: complete && closed, duration_ms: duration, usage: this.usage }),
        })
        this.session = mergeSession(this.session, result)
        this.callbacks.onSession(this.session)
      } catch {
        this.callbacks.onError('No se pudo finalizar la sesión. El historial conservará los resultados recibidos.')
      }
    }
    this.callbacks.onStatus(complete && closed ? 'Sesión guardada' : 'Sesión incompleta guardada')
    } finally { await this.cleanup() }
  }

  private async cleanup() {
    this.intentionalCleanup = true
    this.assistantPlayback?.pause()
    if (this.assistantPlayback) this.assistantPlayback.srcObject = null
    this.microphone?.getTracks().forEach((track) => track.stop())
    this.pcm?.disconnect()
    this.caller?.disconnect()
    this.assistant?.disconnect()
    this.merger?.disconnect()
    this.silentSink?.disconnect()
    this.events?.close()
    this.peer?.close()
    this.socket?.close()
    if (this.context && this.context.state !== 'closed') await this.context.close()
    this.callbacks.onLevel(0)
  }

  dispose() { void this.stop(false) }
}
