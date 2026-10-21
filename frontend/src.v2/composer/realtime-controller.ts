import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult, commandResultSucceeded } from "../protocol/ws-outbox";
import type { RealtimeCapability, RealtimeEvent, RealtimePhase } from "../protocol/realtime-types";
import { pushToast } from "../overlays/ToastContainer";
import type { RealtimeAudio } from "./realtime-audio";
import { desktop, type DesktopVoiceClaim } from "../desktop/runtime";

type StartOptions = { transferId?: string; context?: string; signal?: AbortSignal };

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
  private releasingAudio: Promise<void> | null = null;
  private requested = false;
  private accepted = false;
  private started = false;
  private stopping: Promise<boolean> | null = null;
  private retiring: Promise<void> | null = null;
  private nativeClaim: DesktopVoiceClaim | null = null;
  private activating: Promise<void> | null = null;
  private transferId: string | undefined;
  private unwatch: (() => void) | null = null;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.state;
  getDesktopClaim = () => this.nativeClaim;
  getStartTransferId = () => this.transferId;
  private publish(patch: Partial<RealtimeState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach(listener => listener());
  }
  private activate() {
    if (this.state.phase !== "starting" || !this.started || !this.accepted) return;
    if (!this.nativeClaim) { this.publish({ phase: "active" }); return; }
    if (this.activating) return this.activating;
    const generation = this.generation;
    const claim = this.nativeClaim;
    const work = desktop()!.realtimeVoice!.update({ claimId: claim.claimId, phase: "active" }).then(() => {
      if (this.generation === generation && this.nativeClaim === claim && this.state.phase === "starting") this.publish({ phase: "active" });
    }, async error => {
      if (generation !== this.generation) return;
      pushToast(error instanceof Error ? error.message : String(error), "error");
      await this.stop();
    });
    this.activating = work;
    void work.finally(() => { if (this.activating === work) this.activating = null; });
    return work;
  }
  private async releaseAudio() {
    if (this.releasingAudio) return this.releasingAudio;
    const audio = this.audio;
    if (!audio) return;
    const work = audio.close();
    this.releasingAudio = work;
    try { await work; if (this.audio === audio) this.audio = null; }
    finally { if (this.releasingAudio === work) this.releasingAudio = null; }
  }
  private retire() {
    if (this.retiring) return this.retiring;
    this.generation++;
    this.unwatch?.(); this.unwatch = null;
    this.requested = this.accepted = this.started = false;
    this.publish({ phase: "stopping", level: 0 });
    const claim = this.nativeClaim;
    const work = (async () => {
      await this.releaseAudio();
      if (claim) await desktop()!.realtimeVoice!.release({ claimId: claim.claimId });
      this.nativeClaim = null;
      this.transferId = undefined;
      this.publish({ phase: "inactive", conversationId: null, level: 0, microphoneMuted: false, outputMuted: false });
    })();
    this.retiring = work;
    const clearRetiring = () => { if (this.retiring === work) this.retiring = null; };
    void work.then(clearRetiring, clearRetiring);
    return work;
  }

  async start(capability: RealtimeCapability, options: StartOptions = {}) {
    if (this.state.phase !== "inactive") return;
    options.signal?.throwIfAborted();
    const generation = ++this.generation;
    this.transferId = options.transferId;
    this.publish({ phase: "starting" });
    const cancel = () => { if (generation === this.generation) void this.stop(); };
    options.signal?.addEventListener("abort", cancel, { once: true });
    try {
      const conversationId = await useAppStore.getState().ensureComposerConversation();
      if (generation !== this.generation) return;
      if (!conversationId) { await this.retire(); return; }
      this.publish({ conversationId });
      this.unwatch = useAppStore.subscribe((state, previous) => {
        if (this.state.phase === "starting" && (state.conversationId !== previous.conversationId
          || state.pendingConversationSwitchId !== previous.pendingConversationSwitchId)) void this.stop();
      });
      const voice = desktop()?.realtimeVoice;
      if (voice) {
        const claim = await voice.claim({ threadId: conversationId, transferId: options.transferId });
        if (generation !== this.generation) { await voice.release({ claimId: claim.claimId }); return; }
        this.nativeClaim = claim;
      }
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
        transport: { type: "websocket" }, flushTranscriptTailOnSessionEnd: true,
        ...(this.nativeClaim ? { desktopVoice: this.nativeClaim } : {}),
        ...(options.context ? { initialItems: [{ role: "developer" as const,
          text: `The user transferred this voice conversation from another MiniCode task. Continue naturally using this handoff context: ${options.context}` }] } : {}) }, "thread/realtime/start");
      if (generation !== this.generation) return;
      if (!commandResultSucceeded(result)) throw new Error(result.message);
      this.accepted = true;
      await this.activate();
    } catch (error) {
      if (generation !== this.generation) return;
      pushToast(error instanceof Error ? error.message : String(error), "error");
      await this.stop();
    } finally { options.signal?.removeEventListener("abort", cancel); }
  }

  waitUntilActive(conversationId: string, signal: AbortSignal): Promise<void> {
    signal.throwIfAborted();
    return new Promise((resolve, reject) => {
      const finish = (error?: unknown) => { unsubscribe(); signal.removeEventListener("abort", aborted); if (error) reject(error); else resolve(); };
      const inspect = () => {
        if (this.state.conversationId !== conversationId || !["starting", "active"].includes(this.state.phase)) finish(new Error("The destination voice chat did not start."));
        else if (this.state.phase === "active") finish();
      };
      const aborted = () => finish(signal.reason);
      const unsubscribe = this.subscribe(inspect);
      signal.addEventListener("abort", aborted, { once: true });
      inspect();
    });
  }

  private async markStopping() {
    if (this.nativeClaim) await desktop()!.realtimeVoice!.update({ claimId: this.nativeClaim.claimId, phase: "stopping" });
  }

  stop = (): Promise<boolean> => {
    if (this.stopping) return this.stopping;
    if (this.state.phase === "inactive") return Promise.resolve(true);
    this.generation++;
    this.unwatch?.(); this.unwatch = null;
    this.publish({ phase: "stopping", level: 0 });
    const conversationId = this.state.conversationId;
    const requested = this.requested;
    const work = (async () => {
      try {
        await this.markStopping();
        await this.releaseAudio();
        if (!requested || !conversationId) { await this.retire(); return true; }
        const result = await sendClientCommandAwaitResult({ type: "thread/realtime/stop", threadId: conversationId,
          conversation_id: conversationId }, "thread/realtime/stop", { silent: true });
        if (!commandResultSucceeded(result)) throw new Error(result.message);
        if (this.state.conversationId === conversationId) await this.retire();
        if (this.retiring) await this.retiring;
        return true;
      } catch (error) {
        // The host still owns the call until its close/stop receipt arrives.
        // A successful renderer cleanup is not a server stop receipt.
        if (useAppStore.getState().isConnected) pushToast(error instanceof Error ? error.message : String(error), "error");
        return false;
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
    void this.markStopping();
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
        void this.retire().catch(error => pushToast(error instanceof Error ? error.message : String(error), "error"));
        break;
    }
  }
}

export const realtimeController = new RealtimeController();
