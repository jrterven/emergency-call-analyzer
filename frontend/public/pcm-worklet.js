// This processor only receives the caller's microphone, never assistant audio.
class CallerPCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = Math.round(sampleRate / 4);
    this.buffer = new Int16Array(this.capacity);
    this.offset = 0;
    this.sequence = 0;
    this.totalSamples = 0;
    this.stopped = false;
    this.port.onmessage = ({ data }) => {
      if (data === 'flush') {
        this.flush();
        this.stopped = true;
        this.port.postMessage({ type: 'flushed' });
      }
    };
  }

  flush() {
    if (!this.offset) return;
    const packet = this.buffer.slice(0, this.offset);
    let sum = 0;
    for (let i = 0; i < packet.length; i++) sum += (packet[i] / 32768) ** 2;
    this.port.postMessage({
      type: 'pcm', buffer: packet.buffer, sequence: this.sequence++,
      start_sample: this.totalSamples, samples: packet.length,
      level: Math.sqrt(sum / packet.length),
    }, [packet.buffer]);
    this.totalSamples += packet.length;
    this.offset = 0;
  }

  process(inputs) {
    if (this.stopped) return false;
    const input = inputs[0]?.[0];
    if (input) {
      for (const value of input) {
        const clamped = Math.max(-1, Math.min(1, value));
        this.buffer[this.offset++] = Math.round(clamped * (clamped < 0 ? 32768 : 32767));
        if (this.offset === this.capacity) this.flush();
      }
    }
    return true;
  }
}

registerProcessor('caller-pcm', CallerPCMProcessor);
