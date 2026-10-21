import { useAppStore } from "../stores";
import { realtimeController, type RealtimeController } from "../composer/realtime-controller";
import { threadMetadataRequest } from "../protocol/thread-metadata";
import type { RealtimeCapability } from "../protocol/realtime-types";
import { desktop, type DesktopVoiceRequest } from "./runtime";
import { captureVoicePageContext } from "./voice-page-context";

async function prepareVoiceTarget(threadId: string, originalClaimId: string, signal: AbortSignal, controller: RealtimeController) {
  const state = controller.getSnapshot();
  if (state.phase !== "inactive" && controller.getDesktopClaim()?.claimId !== originalClaimId) throw new Error("The destination has another voice chat in progress.");
  if (useAppStore.getState().pendingConversationSwitchId) throw new Error("A destination task switch is already pending.");
  if (typeof navigator.mediaDevices?.getUserMedia !== "function" || typeof AudioContext === "undefined"
    || typeof AudioWorkletNode === "undefined") throw new Error("The destination window cannot open voice media.");
  const target = await threadMetadataRequest<{ conversationId: string; realtime: RealtimeCapability }>(`/${encodeURIComponent(threadId)}/voice`, { signal });
  signal.throwIfAborted();
  if (target.conversationId !== threadId || !target.realtime.enabled || !target.realtime.configured
    || !target.realtime.transports.includes("websocket")) throw new Error("The destination task has no configured realtime voice transport.");
  return target.realtime;
}

export function switchVoiceConversation(threadId: string, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  const state = useAppStore.getState();
  if (!state.isConnected) return Promise.reject(new Error("The destination session is disconnected."));
  if (state.pendingConversationSwitchId && state.pendingConversationSwitchId !== threadId) return Promise.reject(new Error("Another task switch is pending."));
  if (state.conversationId === threadId && !state.pendingConversationSwitchId) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let admitted = false;
    const finish = (error?: unknown) => { unsubscribe(); signal.removeEventListener("abort", aborted); if (error) reject(error); else resolve(); };
    const inspect = () => {
      if (!admitted) return;
      const current = useAppStore.getState();
      if (!current.isConnected) finish(new Error("The destination session disconnected during task switch."));
      else if (current.conversationId === threadId && !current.pendingConversationSwitchId) finish();
      else if (current.pendingConversationSwitchId !== threadId) finish(new Error("The destination task switch was rejected or replaced."));
    };
    const aborted = () => finish(signal.reason);
    const unsubscribe = useAppStore.subscribe(inspect);
    signal.addEventListener("abort", aborted, { once: true });
    state.requestConversationSwitch(threadId);
    admitted = true;
    inspect();
  });
}

export function installDesktopVoiceControl(controller = realtimeController): () => void {
  const voice = desktop()?.realtimeVoice;
  if (!voice) return () => {};
  const pending = new Map<string, AbortController>();
  const remove = voice.onRequest(request => {
    if (request.type === "cancel") {
      pending.get(request.requestId)?.abort(new Error("The original desktop voice request was cancelled."));
      return;
    }
    const abort = new AbortController();
    pending.set(request.requestId, abort);
    const perform = async (request: Exclude<DesktopVoiceRequest, { type: "cancel" }>) => {
      const signal = abort.signal;
      if (request.type === "capture") return captureVoicePageContext();
      if (request.type === "prepare") {
        const capability = await prepareVoiceTarget(request.threadId!, request.claimId, signal, controller);
        return { prepared: true, capability };
      }
      if (request.type === "stop") {
        if (controller.getDesktopClaim()?.claimId !== request.claimId) throw new Error("The stop request does not own this window's original voice call.");
        const stopped = await controller.stop();
        signal.throwIfAborted();
        return { stopped, mediaReleased: stopped && controller.getSnapshot().phase === "inactive" };
      }
      await switchVoiceConversation(request.threadId!, signal);
      signal.throwIfAborted();
      if (useAppStore.getState().settingsOpen) useAppStore.getState().toggleSettings();
      const cancelStart = () => {
        if (controller.getStartTransferId() === request.transferId) void controller.stop();
      };
      signal.addEventListener("abort", cancelStart, { once: true });
      try {
        await controller.start(request.capability!, { transferId: request.transferId, context: request.context, signal });
        await controller.waitUntilActive(request.threadId!, signal);
        return { started: true };
      } finally { signal.removeEventListener("abort", cancelStart); }
    };
    void perform(request).then(value => {
      if (!abort.signal.aborted) return voice.reply({ requestId: request.requestId, claimId: request.claimId, callId: request.callId, value });
    }, error => {
      if (!abort.signal.aborted) return voice.reply({ requestId: request.requestId, claimId: request.claimId, callId: request.callId,
        error: error instanceof Error ? error.message : String(error) });
    }).finally(() => { pending.delete(request.requestId); });
  });
  const reportReady = () => { void voice.ready({ connected: useAppStore.getState().isConnected }); };
  const unsubscribe = useAppStore.subscribe((state, previous) => { if (state.isConnected !== previous.isConnected) reportReady(); });
  reportReady();
  return () => {
    remove(); unsubscribe();
    for (const request of pending.values()) request.abort(new Error("The desktop voice consumer was detached."));
    void voice.ready({ connected: false });
  };
}
