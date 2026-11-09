class RealtimeCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Float32Array(Math.round(sampleRate / 50));
    this.offset = 0;
  }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const value of channel) {
      this.samples[this.offset++] = value;
      if (this.offset === this.samples.length) {
        const frame = this.samples;
        this.port.postMessage(frame, [frame.buffer]);
        this.samples = new Float32Array(Math.round(sampleRate / 50));
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor("minicode-realtime-capture", RealtimeCapture);
