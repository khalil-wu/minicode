import type { RealtimeAudioChunk } from "../protocol/realtime-types";
import captureWorkletUrl from "./realtime-capture.worklet.js?url&no-inline";

export const encodeRealtimePcm = (samples: Float32Array): string => {
  const bytes = new Uint8Array(samples.length * 2);
  const data = new DataView(bytes.buffer);
  for (let index = 0; index < samples.length; index++) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    data.setInt16(index * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true);
  }
  return btoa(String.fromCharCode(...bytes));
};

/** One user-started call owns these tracks, worklet and scheduled output. */
export class RealtimeAudio {
  private stream: MediaStream | null = null;
  private context: AudioContext | null = null;
  private capture: AudioWorkletNode | null = null;
  private input: MediaStreamAudioSourceNode | null = null;
  private output: GainNode | null = null;
  private sources = new Set<AudioBufferSourceNode>();
  private nextOutputAt = 0;
  private closed = false;

  async open(sampleRate: number, microphoneId: string, onFrame: (audio: RealtimeAudioChunk, level: number) => void) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: {
      ...(microphoneId ? { deviceId: { exact: microphoneId } } : {}),
      channelCount: 1, echoCancellation: true, noiseSuppression: true,
    } });
    if (this.closed) { stream.getTracks().forEach(track => track.stop()); return; }
    this.stream = stream;
    const context = new AudioContext({ sampleRate, latencyHint: "interactive" });
    this.context = context;
    await context.audioWorklet.addModule(captureWorkletUrl);
    if (this.closed) return;
    this.output = context.createGain();
    this.output.connect(context.destination);
    this.input = context.createMediaStreamSource(stream);
    this.capture = new AudioWorkletNode(context, "minicode-realtime-capture");
    this.capture.port.onmessage = ({ data }: MessageEvent<Float32Array>) => {
      if (this.closed) return;
      let sum = 0;
      for (const sample of data) sum += sample * sample;
      onFrame({ data: encodeRealtimePcm(data), sampleRate: context.sampleRate, numChannels: 1,
        samplesPerChannel: data.length }, Math.sqrt(sum / data.length));
    };
    this.input.connect(this.capture);
    // The worklet emits silence; connecting it keeps capture processing live
    // without playing the microphone back through the speakers.
    this.capture.connect(context.destination);
    await context.resume();
  }

  play(audio: RealtimeAudioChunk) {
    const context = this.context;
    if (!context || this.closed) return;
    const binary = atob(audio.data);
    const bytes = Uint8Array.from(binary, value => value.charCodeAt(0));
    const data = new DataView(bytes.buffer);
    const frames = bytes.length / (2 * audio.numChannels);
    const buffer = context.createBuffer(audio.numChannels, frames, audio.sampleRate);
    for (let channel = 0; channel < audio.numChannels; channel++) {
      const samples = buffer.getChannelData(channel);
      for (let index = 0; index < frames; index++) samples[index] = data.getInt16((index * audio.numChannels + channel) * 2, true) / 32768;
    }
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.output!);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    this.sources.add(source);
    const startsAt = Math.max(context.currentTime, this.nextOutputAt);
    source.start(startsAt);
    this.nextOutputAt = startsAt + buffer.duration;
  }

  interruptOutput() {
    for (const source of this.sources) { source.onended = null; source.stop(); source.disconnect(); }
    this.sources.clear();
    this.nextOutputAt = 0;
  }
  setMicrophoneMuted(muted: boolean) { this.stream?.getAudioTracks().forEach(track => { track.enabled = !muted; }); }
  setOutputMuted(muted: boolean) { if (this.output) this.output.gain.value = muted ? 0 : 1; }

  async close() {
    this.closed = true;
    this.stream?.getTracks().forEach(track => track.stop());
    this.capture?.disconnect();
    if (this.capture) this.capture.port.onmessage = null;
    this.input?.disconnect();
    this.interruptOutput();
    this.output?.disconnect();
    if (this.context && this.context.state !== "closed") await this.context.close();
  }
}
