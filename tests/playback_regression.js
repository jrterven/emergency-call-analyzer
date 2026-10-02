// playwright-cli run-code --filename=tests/playback_regression.js
// Synthetic audio and mocked API responses; no calls, microphone or saved history.
async (page) => {
  const origin = /^https?:/.test(page.url()) ? new URL(page.url()).origin : 'http://127.0.0.1:5173'
  const id = '00000000-0000-4000-8000-000000000912'
  const title = 'Prueba de reproducción después de terminar'
  let saved = false
  let polls = 0
  const audioRequests = []
  const unexpectedRequests = []
  const assert = (condition, message) => { if (!condition) throw new Error(message) }
  const wav = (seconds, channels, audible) => {
    const sampleRate = 16000, frames = Math.round(seconds * sampleRate)
    const bytes = Buffer.alloc(44 + frames * channels * 2)
    bytes.write('RIFF', 0); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write('WAVEfmt ', 8)
    bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(channels, 22)
    bytes.writeUInt32LE(sampleRate, 24); bytes.writeUInt32LE(sampleRate * channels * 2, 28)
    bytes.writeUInt16LE(channels * 2, 32); bytes.writeUInt16LE(16, 34)
    bytes.write('data', 36); bytes.writeUInt32LE(bytes.length - 44, 40)
    if (audible) for (let frame = 0; frame < frames; frame++) for (let channel = 0; channel < channels; channel++) {
      const value = Math.round(6500 * Math.sin(2 * Math.PI * (channel ? 660 : 440) * frame / sampleRate))
      bytes.writeInt16LE(value, 44 + (frame * channels + channel) * 2)
    }
    return bytes
  }
  const initialAudio = wav(.25, 1, false)
  const finishedAudio = wav(3, 2, true)
  const fixture = () => ({
    id, source: 'live', title, status: saved ? 'processing' : 'live',
    stage: saved ? 'summarizing' : 'live',
    created_at: '2026-10-02T10:00:00Z', updated_at: saved ? '2026-10-02T10:00:04Z' : '2026-10-02T10:00:00Z',
    duration_ms: saved ? 3000 : 250, channel: 0, filename: saved ? 'recording.wav' : null,
    audio_url: `/api/sessions/${id}/audio`, original_audio_url: null,
    transcript: [], emotions: [], summary: null, warnings: [],
    config: saved ? { recording_artifact: 'recording.wav', recording_audio: { duration_ms: 3000, channels: 2, sample_rate: 16000 } } : {},
  })
  const handler = async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (request.method() !== 'GET') {
      unexpectedRequests.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ status: 409, body: 'Writes forbidden in this test.' })
    }
    if (url.pathname === `/api/sessions/${id}/audio`) {
      audioRequests.push({ saved, url: url.pathname + url.search })
      // Model a previously cached, short caller WAV at the same API URL.
      return route.fulfill({ status: 200, contentType: 'audio/wav',
        headers: { 'Cache-Control': saved ? 'no-store' : 'public, max-age=3600' },
        body: saved ? finishedAudio : initialAudio })
    }
    let response
    if (url.pathname === '/api/health') response = { openai_configured: true, ffmpeg_available: true,
      whisper_model: 'small', emotion_model: 'emotion2vec/emotion2vec_plus_base',
      active_session_id: saved ? null : id, models_loaded: { whisper: false, emotion: false } }
    else if (url.pathname === '/api/sessions') response = [fixture()]
    else if (url.pathname === `/api/sessions/${id}`) { polls++; response = fixture() }
    else {
      unexpectedRequests.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ status: 404, body: 'Unmocked API request.' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) })
  }
  const provider = async (route) => { unexpectedRequests.push('OpenAI request'); await route.abort() }
  await page.route('**/api/**', handler)
  await page.route('https://api.openai.com/**', provider)
  try {
    await page.goto(origin, { waitUntil: 'domcontentloaded' })
    await page.getByRole('navigation', { name: 'Navegación principal' }).getByRole('button', { name: /^Historial/ }).click()
    await page.locator('.history-row').filter({ hasText: title }).click()
    await page.waitForTimeout(500)
    const earlyRequests = audioRequests.length
    saved = true
    await page.waitForFunction(() => {
      const audio = document.querySelector('.playback-panel audio')
      return audio && audio.readyState >= 2 && Math.abs(audio.duration - 3) < .1
    }, null, { timeout: 5000 })
    const playback = await page.evaluate(async () => {
      const audio = document.querySelector('.playback-panel audio')
      audio.__playbackIdentity = 'same-player'
      const context = new AudioContext()
      await context.resume()
      const source = context.createMediaElementSource(audio)
      const analyser = context.createAnalyser(), sink = context.createGain()
      sink.gain.value = 0
      source.connect(analyser).connect(sink).connect(context.destination)
      await audio.play()
      await new Promise(resolve => setTimeout(resolve, 350))
      const samples = new Float32Array(analyser.fftSize)
      analyser.getFloatTimeDomainData(samples)
      const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length)
      audio.pause()
      const result = { duration: audio.duration, currentTime: audio.currentTime, rms, error: audio.error?.code ?? null }
      source.disconnect(); analyser.disconnect(); sink.disconnect(); await context.close()
      return result
    })
    assert(playback.rms > .03 && playback.currentTime > .2 && playback.error === null,
      `No audible playback: ${JSON.stringify(playback)}`)
    const pollCount = polls
    await page.waitForTimeout(1300)
    const stable = await page.evaluate(() => {
      const audio = document.querySelector('.playback-panel audio')
      return audio?.__playbackIdentity === 'same-player' && Math.abs(audio.currentTime - .35) < .2
    })
    assert(polls > pollCount && stable, 'Session polling replaced or reset the audio player.')
    assert(earlyRequests === 0, 'The player fetched an unfinished recording during the call.')
    assert(unexpectedRequests.length === 0, `Unexpected requests: ${unexpectedRequests.join(', ')}`)
    return { passed: true, earlyRequests, audioRequests, playback, stable }
  } finally {
    // Stop the fixture's polling before removing interception.
    await page.goto('about:blank')
    await page.unroute('**/api/**', handler)
    await page.unroute('https://api.openai.com/**', provider)
  }
}
