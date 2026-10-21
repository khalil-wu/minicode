/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { useAppStore } from "../stores";
import { RealtimeController } from "../composer/realtime-controller";
import { installDesktopVoiceControl, switchVoiceConversation } from "./voice-control";
import type { DesktopVoiceRequest } from "./runtime";
import type { RealtimeCapability } from "../protocol/realtime-types";

const fixture = vi.hoisted(() => ({ send: vi.fn(() => true), result: vi.fn(), metadata: vi.fn(), toast: vi.fn(),
  open: vi.fn(), close: vi.fn(), reply: vi.fn(), claim: vi.fn(), update: vi.fn(), release: vi.fn(), ready: vi.fn(),
  request: null as null | ((request: DesktopVoiceRequest) => void) }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<object>(), sendClientCommand: fixture.send, sendClientCommandAwaitResult: fixture.result }));
vi.mock("../protocol/thread-metadata", () => ({ threadMetadataRequest: fixture.metadata }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: fixture.toast }));
vi.mock("../composer/realtime-audio", () => ({ RealtimeAudio: class {
  open = fixture.open; close = fixture.close; setMicrophoneMuted = vi.fn(); setOutputMuted = vi.fn(); play = vi.fn(); interruptOutput = vi.fn();
} }));

const capability: RealtimeCapability = { enabled: true, configured: true, transports: ["websocket"], version: "v2", model: "actual-realtime", inputSampleRate: 24000, inputChannels: 1 };
const success = (command: string) => ({ type: "command.result", command, level: "success", message: "", data: {} });
const request = (type: "capture" | "prepare" | "stop" | "start", patch: Partial<Exclude<DesktopVoiceRequest, { type: "cancel" }>> = {}) => {
  const value = { type, requestId: type + "-request", claimId: "claim-A", callId: "original-model-call", ...patch };
  fixture.request!(value);
  return value;
};
const controllers: RealtimeController[] = [];
const cleanups: Array<() => void> = [];

beforeEach(() => {
  vi.clearAllMocks();
  fixture.open.mockResolvedValue(undefined); fixture.close.mockResolvedValue(undefined);
  fixture.send.mockReturnValue(true); fixture.result.mockImplementation(async command => success(command.type));
  fixture.reply.mockResolvedValue(true); fixture.update.mockResolvedValue(undefined); fixture.release.mockResolvedValue(undefined); fixture.ready.mockResolvedValue(undefined);
  fixture.claim.mockImplementation(async ({ threadId }) => ({ claimId: "claim-" + threadId, hostId: "local", captureScreenContext: true, transferVoiceCall: true }));
  fixture.metadata.mockResolvedValue({ conversationId: "B", realtime: capability });
  window.__MINICODE_RUNTIME__ = { desktop: { realtimeVoice: { claim: fixture.claim, update: fixture.update, release: fixture.release,
    ready: fixture.ready, reply: fixture.reply, onRequest: (callback: (request: DesktopVoiceRequest) => void) => { fixture.request = callback; return () => { fixture.request = null; }; } } } as never };
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn() } });
  vi.stubGlobal("AudioContext", class {}); vi.stubGlobal("AudioWorkletNode", class {});
  useAppStore.setState({ conversationId: "A", isConnected: true, pendingConversationSwitchId: null, pendingConversationCreateId: null,
    pendingComposerConversationAdoption: null, messages: [], conversationMessages: {}, conversationAgentStates: {}, conversationWorkbenchStates: {},
    draft: "A unsaved draft", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null,
    isStreaming: false, settingsOpen: false, projectsOpen: false, skillsMarketplaceOpen: false,
    conversations: [{ id: "A", title: "Original" }, { id: "B", title: "Target" }] as never, workingDirectory: "" });
});
afterEach(async () => {
  fixture.result.mockImplementation(async command => success(command.type));
  for (const controller of controllers.splice(0)) await controller.stop();
  for (const stop of cleanups.splice(0)) stop();
  delete window.__MINICODE_RUNTIME__; vi.unstubAllGlobals();
});

async function activeCall() {
  const controller = new RealtimeController(); controllers.push(controller);
  cleanups.push(installDesktopVoiceControl(controller));
  await controller.start(capability);
  controller.handleEvent({ type: "thread/realtime/started", threadId: "A", conversation_id: "A", version: "v2", realtimeSessionId: "actual-A" });
  await waitFor(() => expect(controller.getSnapshot().phase).toBe("active"));
  return controller;
}

describe("native original claim to real controller and canonical task switch", () => {
  it("keeps the claim private in Source start and waits for audio release, stop receipt, switch and target started", async () => {
    const controller = await activeCall();
    expect(fixture.result).toHaveBeenCalledWith(expect.objectContaining({ type: "thread/realtime/start",
      desktopVoice: { claimId: "claim-A", hostId: "local", captureScreenContext: true, transferVoiceCall: true } }), "thread/realtime/start");
    request("prepare", { threadId: "B" });
    await waitFor(() => expect(fixture.reply).toHaveBeenCalledWith(expect.objectContaining({ requestId: "prepare-request", value: { prepared: true, capability } })));
    expect(fixture.metadata).toHaveBeenCalledWith("/B/voice", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(controller.getSnapshot().conversationId).toBe("A");
    let acceptStop!: (value: unknown) => void;
    fixture.result.mockImplementationOnce(() => new Promise(resolve => { acceptStop = resolve; }));
    request("stop");
    await waitFor(() => expect(fixture.result).toHaveBeenLastCalledWith(expect.objectContaining({ type: "thread/realtime/stop" }), "thread/realtime/stop", { silent: true }));
    expect(fixture.close).toHaveBeenCalledOnce();
    expect(fixture.reply).not.toHaveBeenCalledWith(expect.objectContaining({ requestId: "stop-request" }));
    expect(useAppStore.getState().draft).toBe("A unsaved draft");
    acceptStop(success("thread/realtime/stop"));
    await waitFor(() => expect(fixture.reply).toHaveBeenCalledWith(expect.objectContaining({ requestId: "stop-request", value: { stopped: true, mediaReleased: true } })));
    expect(fixture.release).toHaveBeenCalledWith({ claimId: "claim-A" });
    request("start", { threadId: "B", transferId: "native-transfer", context: "Actual spoken context", capability });
    await waitFor(() => expect(useAppStore.getState().pendingConversationSwitchId).toBe("B"));
    expect(useAppStore.getState().conversationId).toBe("A");
    expect(fixture.send).toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.switch", conversation_id: "B" }));
    expect(fixture.claim).not.toHaveBeenCalledWith(expect.objectContaining({ threadId: "B" }));
    useAppStore.getState().applyConversationSwitched({ conversationId: "B" });
    await waitFor(() => expect(fixture.result).toHaveBeenLastCalledWith(expect.objectContaining({ type: "thread/realtime/start", threadId: "B",
      initialItems: [{ role: "developer", text: "The user transferred this voice conversation from another MiniCode task. Continue naturally using this handoff context: Actual spoken context" }] }), "thread/realtime/start"));
    expect(fixture.claim).toHaveBeenLastCalledWith({ threadId: "B", transferId: "native-transfer" });
    expect(useAppStore.getState().conversationWorkbenchStates.A.draft).toBe("A unsaved draft");
    expect(fixture.reply).not.toHaveBeenCalledWith(expect.objectContaining({ requestId: "start-request" }));
    controller.handleEvent({ type: "thread/realtime/started", threadId: "B", conversation_id: "B", version: "v2", realtimeSessionId: "actual-B" });
    await waitFor(() => expect(fixture.reply).toHaveBeenCalledWith(expect.objectContaining({ requestId: "start-request", value: { started: true } })));
    expect(fixture.update).toHaveBeenLastCalledWith({ claimId: "claim-B", phase: "active" });
  });

  it("preflight refusal preserves original media, owner, and draft", async () => {
    const controller = await activeCall();
    fixture.metadata.mockRejectedValueOnce(new Error("Target was archived"));
    request("prepare", { threadId: "B" });
    await waitFor(() => expect(fixture.reply).toHaveBeenCalledWith(expect.objectContaining({ requestId: "prepare-request", error: "Target was archived" })));
    expect(controller.getSnapshot()).toMatchObject({ phase: "active", conversationId: "A" });
    expect(fixture.close).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("A unsaved draft");
  });

  it("failed backend stop never releases the native claim or replies stopped true", async () => {
    const controller = await activeCall();
    fixture.result.mockResolvedValueOnce({ ...success("thread/realtime/stop"), level: "error", message: "Actual stop rejected" });
    request("stop");
    await waitFor(() => expect(fixture.reply).toHaveBeenCalledWith(expect.objectContaining({ requestId: "stop-request", value: { stopped: false, mediaReleased: false } })));
    expect(fixture.release).not.toHaveBeenCalled();
    expect(controller.getSnapshot().phase).toBe("stopping");
    expect(fixture.toast).toHaveBeenCalledWith("Actual stop rejected", "error");
  });

  it("awaits a real close of the media device even when backend closed races the stop receipt", async () => {
    const controller = await activeCall();
    let releaseAudio!: () => void;
    fixture.close.mockImplementationOnce(() => new Promise<void>(resolve => { releaseAudio = resolve; }));
    const stopping = controller.stop();
    await waitFor(() => expect(fixture.close).toHaveBeenCalledOnce());
    controller.handleEvent({ type: "thread/realtime/closed", threadId: "A", conversation_id: "A", reason: "stopped" });
    expect(fixture.release).not.toHaveBeenCalled();
    releaseAudio();
    expect(await stopping).toBe(true);
    expect(fixture.release).toHaveBeenCalledOnce();
    expect(controller.getSnapshot().phase).toBe("inactive");
  });

  it("media release failure preserves its claim and media handle until a successful retry", async () => {
    const controller = await activeCall();
    fixture.close.mockRejectedValueOnce(new Error("Actual audio device close failed"));
    expect(await controller.stop()).toBe(false);
    expect(fixture.release).not.toHaveBeenCalled();
    expect(controller.getDesktopClaim()?.claimId).toBe("claim-A");
    expect(fixture.result).toHaveBeenCalledTimes(1);
    expect(controller.getSnapshot().phase).toBe("stopping");
    expect(await controller.stop()).toBe(true);
    expect(fixture.close).toHaveBeenCalledTimes(2);
    expect(fixture.release).toHaveBeenCalledWith({ claimId: "claim-A" });
  });

  it("cancelled native task switching preserves owners and prevents a target start", async () => {
    const controller = await activeCall(); await controller.stop();
    request("start", { threadId: "B", transferId: "native-transfer", capability });
    await waitFor(() => expect(useAppStore.getState().pendingConversationSwitchId).toBe("B"));
    fixture.request!({ type: "cancel", requestId: "start-request" });
    useAppStore.getState().applyConversationSwitched({ conversationId: "B" });
    await Promise.resolve(); await Promise.resolve();
    expect(fixture.claim).not.toHaveBeenCalledWith(expect.objectContaining({ threadId: "B" }));
    expect(fixture.reply).not.toHaveBeenCalledWith(expect.objectContaining({ requestId: "start-request" }));
    expect(useAppStore.getState().conversationWorkbenchStates.A.draft).toBe("A unsaved draft");
  });

  it("rejects replaced and disconnected canonical switch receipts", async () => {
    const pending = switchVoiceConversation("B", new AbortController().signal);
    const replaced = expect(pending).rejects.toThrow("rejected or replaced");
    useAppStore.setState({ pendingConversationSwitchId: "C" });
    await replaced;
    useAppStore.setState({ pendingConversationSwitchId: null });
    const disconnecting = switchVoiceConversation("B", new AbortController().signal);
    const disconnected = expect(disconnecting).rejects.toThrow("disconnected");
    useAppStore.setState({ isConnected: false });
    await disconnected;
    expect(useAppStore.getState().conversationId).toBe("A");
  });
});
