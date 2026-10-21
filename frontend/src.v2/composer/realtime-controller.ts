import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult, commandResultSucceeded } from "../protocol/ws-outbox";
import type { RealtimeCapability, RealtimeEvent, RealtimePhase } from "../protocol/realtime-types";
import { pushToast } from "../overlays/ToastContainer";
import type { RealtimeAudio } from "./realtime-audio";

type RealtimeState = {
  phase: RealtimePhase;
  conversationId: string | null;
  microphoneMuted: boolean;
  outputMuted: boolean;
  level: number;
};

export class RealtimeController {
  private state: RealtimeState = { phase: "inactive", conversationId: null, microphoneMuted: false, outputMuted: false, level: 0 };
  private listeners = new Set<() => void>();
  private generation = 0;
  private audio: RealtimeAudio | null = null;
  private requested = false;
  private accepted = false;
  private started = false;
  private stopping: Promise<void> | null = null;
  private unwatch: (() => void) | null = null;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.state;
  private publish(patch: Partial<RealtimeState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach(listener => listener());
  }
  private activate() {
    if (this.state.phase === "starting" && this.started && this.accepted) this.publish({ phase: "active" });
  }
  private async releaseAudio() {
    const audio = this.audio;
    this.audio = null;
    await audio?.close();
  }
  private retire() {
    this.generation++;
    this.unwatch?.(); this.unwatch = null;
    this.requested = this.accepted = this.started = false;
    this.publish({ phase: "inactive", conversationId: null, level: 0, microphoneMuted: false, outputMuted: false });
  }

  async start(capability: RealtimeCapability) {
    if (this.state.phase !== "inactive") return;
    const generation = ++this.generation;
    this.publish({ phase: "starting" });
    try {
      const conversationId = await useAppStore.getState().ensureComposerConversation();
      if (generation !== this.generation) return;
      if (!conversationId) { this.retire(); return; }
      this.publish({ conversationId });
      this.unwatch = useAppStore.subscribe((state, previous) => {
        if (this.state.phase === "starting" && (state.conversationId !== previous.conversationId
          || state.pendingConversationSwitchId !== previous.pendingConversationSwitchId)) void this.stop();
      });
      const { RealtimeAudio } = await import("./realtime-audio");
      if (generation !== this.generation) return;
      const audio = new RealtimeAudio();
      this.audio = audio;
      await audio.open(capability.inputSampleRate, useAppStore.getState().workbenchPreferences.microphoneId, (frame, level) => {
        if (generation !== this.generation || this.state.phase !== "active" || this.state.microphoneMuted) return;
        if (!sendClientCommand({ type: "thread/realtime/appendAudio", threadId: conversationId, conversation_id: conversationId, audio: frame }, { silent: true })) {
          this.onTransportClosed();
          return;
        }
        this.publish({ level });
      });
      if (generation !== this.generation) return;
      this.requested = true;
      const result = await sendClientCommandAwaitResult({ type: "thread/realtime/start", threadId: conversationId,
        conversation_id: conversationId, outputModality: "audio", version: capability.version,
        transport: { type: "websocket" }, flushTranscriptTailOnSessionEnd: true }, "thread/realtime/start");
      if (generation !== this.generation) return;
      if (!commandResultSucceeded(result)) throw new Error(result.message);
      this.accepted = true;
      this.activate();
    } catch (error) {
      if (generation !== this.generation) return;
      pushToast(error instanceof Error ? error.message : String(error), "error");
      await this.stop();
    }
  }

  stop = (): Promise<void> => {
    if (this.stopping) return this.stopping;
    if (this.state.phase === "inactive") return Promise.resolve();
    this.generation++;
    this.unwatch?.(); this.unwatch = null;
    this.publish({ phase: "stopping", level: 0 });
    const conversationId = this.state.conversationId;
    const requested = this.requested;
    const work = (async () => {
      await this.releaseAudio();
      if (!requested || !conversationId) { this.retire(); return; }
      try {
        const result = await sendClientCommandAwaitResult({ type: "thread/realtime/stop", threadId: conversationId,
          conversation_id: conversationId }, "thread/realtime/stop", { silent: true });
        if (!commandResultSucceeded(result)) throw new Error(result.message);
        if (this.state.conversationId === conversationId) this.retire();
      } catch (error) {
        // The host still owns the call until its close/stop receipt arrives.
        // A successful renderer cleanup is not a server stop receipt.
        if (useAppStore.getState().isConnected) pushToast(error instanceof Error ? error.message : String(error), "error");
      }
    })();
    this.stopping = work;
    void work.finally(() => { if (this.stopping === work) this.stopping = null; });
    return work;
  };

  toggleMicrophone = () => {
    const muted = !this.state.microphoneMuted;
    this.audio?.setMicrophoneMuted(muted);
    this.publish({ microphoneMuted: muted, ...(muted ? { level: 0 } : {}) });
  };
  toggleOutput = () => {
    const muted = !this.state.outputMuted;
    this.audio?.setOutputMuted(muted);
    this.publish({ outputMuted: muted });
  };
  onTransportClosed = () => {
    if (this.state.phase === "inactive") return;
    this.generation++;
    this.unwatch?.(); this.unwatch = null;
    this.publish({ phase: "stopping", level: 0 });
    void this.releaseAudio();
  };
  onTransportReady = () => { if (this.state.phase === "stopping") void this.stop(); };
  handleEvent(event: RealtimeEvent) {
    if (event.conversation_id !== this.state.conversationId) return;
    switch (event.type) {
      case "thread/realtime/started": this.started = true; this.activate(); break;
      case "thread/realtime/outputAudio/delta": if (this.state.phase === "active") this.audio?.play(event.audio); break;
      case "thread/realtime/itemAdded":
        if (event.item.type === "input_audio_buffer.speech_started") this.audio?.interruptOutput();
        break;
      case "thread/realtime/error":
        pushToast(event.message, "error");
        void this.stop();
        break;
      case "thread/realtime/closed":
        void this.releaseAudio();
        this.retire();
        break;
    }
  }
}

export const realtimeController = new RealtimeController();
