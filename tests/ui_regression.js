// Run in the existing Vite tab with playwright-cli run-code --filename.
// Every API request is intercepted. No microphone, provider or saved history is used.
async (page) => {
  const started = Date.now()
  const originalViewport = page.viewportSize()
  const appOrigin = /^https?:/.test(page.url()) ? new URL(page.url()).origin : 'http://localhost:5173'
  const sessionId = '00000000-0000-4000-8000-000000000911'
  const title = 'Regresión de llamada simulada en español'
  const initialTranscript = []
  for (let turn = 0; turn < 40; turn++) {
    const parts = turn % 2 === 0
      ? ['Estoy en la avenida Reforma, ', 'junto a la estación del metro. ', 'Hay humo y dos personas necesitan ayuda. ']
      : ['Entiendo. Mantén una distancia segura. ', '¿Puedes decirme si las personas están conscientes? ', 'Voy a registrar lo que me estás contando. ']
    parts.forEach((text, part) => initialTranscript.push({
      id: `initial-${turn}-${part}`, speaker: turn % 2 === 0 ? 'caller' : 'assistant',
      text, start_ms: turn * 3000 + part * 300, end_ms: turn * 3000 + (part + 1) * 300,
    }))
  }
  const scores = { angry: .03, disgusted: .01, fearful: .55, happy: .01, neutral: .2, other: .02, sad: .1, surprised: .06, unknown: .02 }
  const emotion = (index) => ({ id: `emotion-${index}`, start_ms: index * 2000, end_ms: index * 2000 + 4000,
    scores, label: 'fearful', status: 'ok', latency_ms: 85, voiced_ms: 3200 })
  const initial = {
    id: sessionId, source: 'live', title, status: 'live', stage: 'live',
    created_at: '2026-10-01T10:00:00.000Z', updated_at: '2026-10-01T10:02:00.000Z',
    duration_ms: 120000, channel: 0, filename: null, audio_url: null, original_audio_url: null,
    transcript: initialTranscript, emotions: [emotion(0)], summary: null, warnings: [],
    config: { live_model: 'gpt-live-1', emotion_model: 'emotion2vec/emotion2vec_plus_base' },
  }
  let snapshots = 0
  let fullSnapshots = 0
  let emptySnapshots = 0
  const unexpectedRequests = []
  const routeHandler = async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let response
    if (request.method() !== 'GET') {
      unexpectedRequests.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'API writes are forbidden in this regression.' }) })
    }
    if (url.pathname === '/api/health') response = {
      openai_configured: true, ffmpeg_available: true, whisper_model: 'small',
      emotion_model: 'emotion2vec/emotion2vec_plus_base', active_session_id: null,
      models_loaded: { whisper: true, emotion: true },
    }
    else if (url.pathname === '/api/sessions') response = [initial]
    else if (url.pathname === `/api/sessions/${sessionId}`) {
      snapshots++
      if (snapshots % 2 === 0) {
        emptySnapshots++
        // Simulate a delayed snapshot taken before the optimistic Live deltas.
        response = { ...initial, status: 'created', stage: 'created', duration_ms: 0,
          updated_at: '2026-10-01T10:00:00.000Z', transcript: [], emotions: [] }
      } else {
        fullSnapshots++
        const additions = Array.from({ length: fullSnapshots - 1 }, (_, index) => ({
          id: `delta-${index}`, speaker: index % 2 === 0 ? 'caller' : 'assistant',
          text: ` Actualización ${index + 1}: seguimos esperando ayuda en la avenida Reforma.`,
          start_ms: 120000 + index * 3000, end_ms: 122000 + index * 3000,
        }))
        response = { ...initial, updated_at: `2026-10-01T10:02:${String(fullSnapshots).padStart(2, '0')}.000Z`,
          duration_ms: 120000 + additions.length * 3000, transcript: [...initialTranscript, ...additions],
          emotions: Array.from({ length: fullSnapshots }, (_, index) => emotion(index)) }
      }
    } else {
      unexpectedRequests.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ detail: 'Unmocked API request.' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) })
  }
  const providerRequests = []
  const providerHandler = async (route) => { providerRequests.push(route.request().url()); await route.abort() }
  const assert = (condition, message) => { if (!condition) throw new Error(message) }
  const waitForSnapshots = async (target) => {
    while (snapshots < target) {
      assert(Date.now() - started < 11000, `Polling stalled at ${snapshots}/${target} snapshots.`)
      await page.waitForTimeout(75)
    }
    // Allow React effects and animation-frame measurements to observe the response.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
  }
  await page.route('**/api/**', routeHandler)
  await page.route('https://api.openai.com/**', providerHandler)
  try {
    await page.setViewportSize({ width: 1280, height: 720 })
    await page.goto(appOrigin, { waitUntil: 'domcontentloaded', timeout: 5000 })
    await page.getByRole('navigation', { name: 'Navegación principal' }).getByRole('button', { name: /^Historial/ }).click({ timeout: 3000 })
    await page.locator('.history-row').filter({ hasText: title }).click({ timeout: 3000 })
    await page.locator('.transcript-item').first().waitFor({ timeout: 3000 })
    const initialView = await page.evaluate(() => {
      const viewport = document.querySelector('.transcript-scroll')
      return { rows: document.querySelectorAll('.transcript-item').length,
        overflow: viewport.scrollHeight - viewport.clientHeight, text: viewport.textContent }
    })
    assert(initialView.rows === 40, `Fragment grouping produced ${initialView.rows} turns instead of 40.`)
    assert(initialView.text.includes('Estoy en la avenida Reforma, junto a la estación del metro.'), 'Spanish provider fragments lost their original spacing.')
    assert(initialView.overflow > 400, 'Fixture must overflow the transcript viewport to exercise manual scroll.')

    // Check the actual provider-event handler without opening any media or network connection.
    const providerCheck = await page.evaluate(async (fixture) => {
      const { LiveCall } = await import(`/src/lib/live.ts?ui-regression=${Date.now()}`)
      const { mergeSession } = await import(`/src/lib/sessionState.ts?ui-regression=${Date.now()}`)
      const updates = []
      const sent = []
      const call = new LiveCall({ onSession: session => updates.push(session), onStatus: () => {}, onLevel: () => {}, onError: () => {} })
      call.session = structuredClone(fixture)
      call.socket = { readyState: WebSocket.OPEN, send: message => sent.push(JSON.parse(message)) }
      const event = { type: 'session.input_transcript.delta', event_id: 'optimistic-911',
        delta: ' Hay un incendio cerca de la estación.', start_ms: 120000, end_ms: 121000 }
      call.handleProviderEvent(JSON.stringify(event))
      call.handleProviderEvent(JSON.stringify(event))
      const optimistic = updates.at(-1)
      const staleEmotionSnapshot = { ...fixture, transcript: [], emotions: [{
        id: 'late-emotion', start_ms: 120000, end_ms: 124000, scores: { fearful: .6 },
        label: 'fearful', status: 'ok', latency_ms: 80, voiced_ms: 3100,
      }] }
      const merged = mergeSession(optimistic, staleEmotionSnapshot)
      const differentSession = mergeSession(merged, { ...fixture, id: 'other-session', transcript: [], emotions: [] })
      return { callbacks: updates.length, text: merged.transcript.at(-1)?.text,
        count: merged.transcript.length, emotions: merged.emotions.length,
        sent: sent.filter(item => item.type === 'transcript').length,
        differentSessionFragments: differentSession.transcript.length }
    }, initial)
    assert(providerCheck.callbacks === 1, 'Duplicate provider event emitted an extra optimistic transcript update.')
    assert(providerCheck.count === initialTranscript.length + 1 && providerCheck.text === ' Hay un incendio cerca de la estación.', 'A stale emotion snapshot erased or changed an optimistic provider fragment.')
    assert(providerCheck.emotions === 2, 'Merging a late snapshot discarded an emotional window.')
    assert(providerCheck.sent === 2, 'Provider transcript events were not forwarded to local persistence.')
    assert(providerCheck.differentSessionFragments === 0, 'Switching sessions carried fragments from the prior call.')

    await page.evaluate(() => {
      const panel = document.querySelector('.timeline-panel')
      window.scrollTo(0, panel.getBoundingClientRect().top + window.scrollY - 120)
      const regression = { emptyFrames: 0, emptyMutations: 0, windowDrift: 0, transcriptDrift: 0,
        windowY: window.scrollY, transcriptY: null, phase: 'signals', stopped: false }
      window.__emergencyUiRegression = regression
      const observer = new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
          if (node instanceof Element && (node.matches('.transcript-placeholder') || node.querySelector('.transcript-placeholder'))) regression.emptyMutations++
        }
      })
      observer.observe(document.querySelector('.transcript-panel'), { childList: true, subtree: true })
      regression.observer = observer
      const sample = () => {
        if (regression.stopped) return
        if (document.querySelector('.transcript-panel .transcript-placeholder')) regression.emptyFrames++
        regression.windowDrift = Math.max(regression.windowDrift, Math.abs(window.scrollY - regression.windowY))
        if (regression.phase === 'manual') regression.transcriptDrift = Math.max(regression.transcriptDrift,
          Math.abs(document.querySelector('.transcript-scroll').scrollTop - regression.transcriptY))
        requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
    })
    await waitForSnapshots(4)
    const signals = await page.evaluate(() => ({
      y: window.__emergencyUiRegression.windowY, drift: window.__emergencyUiRegression.windowDrift,
      emptyFrames: window.__emergencyUiRegression.emptyFrames, emptyMutations: window.__emergencyUiRegression.emptyMutations,
      hasNewText: document.querySelector('.transcript-scroll').textContent.includes('Actualización 1:'),
      status: document.querySelector('.call-panel .badge').textContent,
    }))
    assert(signals.y > 0, 'Signals scroll assertion did not exercise a scrolled document.')
    assert(signals.drift <= 2, `Document jumped ${signals.drift}px while viewing the emotion chart.`)
    assert(signals.emptyFrames === 0 && signals.emptyMutations === 0, 'Transcript flashed its empty placeholder after a stale polling response.')
    assert(signals.hasNewText, 'New full polling snapshot failed to append its transcript delta.')
    assert(signals.status.includes('En curso'), 'An older snapshot regressed the live session status.')

    const manual = await page.evaluate(() => {
      const viewport = document.querySelector('.transcript-scroll')
      window.scrollTo(0, document.querySelector('.transcript-panel').getBoundingClientRect().top + window.scrollY - 120)
      viewport.scrollTop = 100
      viewport.dispatchEvent(new Event('scroll', { bubbles: true }))
      const regression = window.__emergencyUiRegression
      regression.phase = 'manual'; regression.windowY = window.scrollY; regression.windowDrift = 0
      regression.transcriptY = viewport.scrollTop
      return { top: viewport.scrollTop, gap: viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop }
    })
    assert(manual.top > 0 && manual.gap > 400, 'Manual scroll setup must leave the reader far from the newest fragment.')
    await waitForSnapshots(7)
    const final = await page.evaluate(() => {
      const regression = window.__emergencyUiRegression
      regression.stopped = true; regression.observer.disconnect()
      return { windowDrift: regression.windowDrift, transcriptDrift: regression.transcriptDrift,
        emptyFrames: regression.emptyFrames, emptyMutations: regression.emptyMutations,
        newestVisibleText: document.querySelector('.transcript-scroll').textContent.includes('Actualización 3:') }
    })
    assert(final.newestVisibleText, 'No new delta arrived after scrolling the transcript manually.')
    assert(final.transcriptDrift <= 2, `Transcript jumped ${final.transcriptDrift}px after the reader scrolled upward.`)
    assert(final.windowDrift <= 2, `Document jumped ${final.windowDrift}px during manual transcript review.`)
    assert(final.emptyFrames === 0 && final.emptyMutations === 0, 'An empty transcript appeared during incremental updates.')
    assert(fullSnapshots >= 4 && emptySnapshots >= 3, 'Both new and stale snapshots must be observed repeatedly.')
    assert(unexpectedRequests.length === 0, `Unexpected API requests: ${unexpectedRequests.join(', ')}`)
    assert(providerRequests.length === 0, 'The regression attempted an OpenAI network request.')
    return { passed: true, snapshots, fullSnapshots, emptySnapshots, providerCheck,
      signalsWindowDrift: signals.drift, manualWindowDrift: final.windowDrift,
      manualTranscriptDrift: final.transcriptDrift, emptyPlaceholderFrames: final.emptyFrames,
      emptyPlaceholderMutations: final.emptyMutations, elapsed_ms: Date.now() - started }
  } finally {
    // Stop React polling before removing routes; even an assertion failure stays isolated.
    await page.goto('about:blank')
    await page.unroute('**/api/**', routeHandler)
    await page.unroute('https://api.openai.com/**', providerHandler)
    if (originalViewport) await page.setViewportSize(originalViewport)
  }
}
