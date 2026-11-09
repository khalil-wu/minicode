/* @vitest-environment jsdom */
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { handleRealtimeEvent } from "../chat/realtimeEvents";
import { hydrateMessages } from "../chat/transcriptHydration";
import { projectMessagesToTurns } from "../chat/chatSurfaceState";
import { RealtimeController, realtimeController } from "./realtime-controller";
import { RealtimeComposerActions } from "./RealtimeComposerActions";
import type { RealtimeAudioChunk, RealtimeCapability, RealtimeEvent, RealtimeItem } from "../protocol/realtime-types";

const fixtures = vi.hoisted(() => ({
  send: vi.fn(() => true), awaitResult: vi.fn(), toast: vi.fn(),
  open: vi.fn(), close: vi.fn(), play: vi.fn(), microphone: vi.fn(), output: vi.fn(),
  frames: null as null | ((frame: RealtimeAudioChunk, level: number) => void),
}));
vi.mock("../protocol/ws-outbox", async importOriginal => ({ ...await importOriginal<object>(),
  sendClientCommand: fixtures.send, sendClientCommandAwaitResult: fixtures.awaitResult }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: fixtures.toast }));
vi.mock("./realtime-audio", () => ({ RealtimeAudio: class {
  open = async (...args: [number, string, (frame: RealtimeAudioChunk, level: number) => void]) => {
    fixtures.frames = args[2]; await fixtures.open(...args);
  };
  close = fixtures.close;
  play = fixtures.play;
  setMicrophoneMuted = fixtures.microphone;
  setOutputMuted = fixtures.output;
  interruptOutput = vi.fn();
} }));

const capability: RealtimeCapability = { enabled: true, configured: true, transports: ["websocket"],
  version: "v3", model: "configured-realtime-model", inputSampleRate: 24000, inputChannels: 1 };
const frame: RealtimeAudioChunk = { data: "AAAAAA==", sampleRate: 24000, numChannels: 1, samplesPerChannel: 2 };
const started: RealtimeEvent = { type: "thread/realtime/started", threadId: "A", conversation_id: "A", version: "v3", realtimeSessionId: "session-A" };
const success = (command: string) => ({ type: "command.result", command, level: "success", message: "", data: {} });
const item = (id: string, text: string, role: "user" | "assistant" = "assistant"): RealtimeItem => ({
  id, type: "transcriptSegment", realtimeSessionId: "session-A", role, text,
});

beforeEach(() => {
  vi.clearAllMocks();
  fixtures.open.mockResolvedValue(undefined);
  fixtures.close.mockResolvedValue(undefined);
  fixtures.send.mockReturnValue(true);
  fixtures.awaitResult.mockImplementation(async (command: { type: string }) => success(command.type));
  useAppStore.setState({ conversationId: "A", conversationMessages: {}, messages: [], isStreaming: false,
    isConnected: true, draft: "", attachments: [], selectedMentions: [], quotedMessage: null, goalMode: false,
    pendingConversationCreateId: null, pendingConversationSwitchId: null, runtimeCapabilities: { realtime: capability } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("actual realtime owner and media admission", () => {
  it("activates only after both host receipts and keeps live frames on A while B is selected", async () => {
    const controller = new RealtimeController();
    let accept!: (value: unknown) => void;
    fixtures.awaitResult.mockImplementationOnce(() => new Promise(resolve => { accept = resolve; }));
    const starting = controller.start(capability);
    await waitFor(() => expect(fixtures.awaitResult).toHaveBeenCalledOnce());
    controller.handleEvent(started);
    expect(controller.getSnapshot().phase).toBe("starting");
    fixtures.frames!(frame, .3);
    expect(fixtures.send).not.toHaveBeenCalled();
    accept(success("thread/realtime/start"));
    await starting;
    expect(controller.getSnapshot().phase).toBe("active");
    useAppStore.setState({ conversationId: "B", draft: "B unsaved draft" });
    fixtures.frames!(frame, .3);
    expect(fixtures.send).toHaveBeenLastCalledWith(expect.objectContaining({ type: "thread/realtime/appendAudio",
      threadId: "A", conversation_id: "A", audio: frame }), { silent: true });
    controller.handleEvent({ type: "thread/realtime/outputAudio/delta", threadId: "B", conversation_id: "B", audio: frame });
    expect(fixtures.play).not.toHaveBeenCalled();
    controller.handleEvent({ type: "thread/realtime/outputAudio/delta", threadId: "A", conversation_id: "A", audio: frame });
    expect(fixtures.play).toHaveBeenCalledWith(frame);
    controller.toggleMicrophone(); controller.toggleOutput();
    expect(fixtures.microphone).toHaveBeenCalledWith(true);
    expect(fixtures.output).toHaveBeenCalledWith(true);
    await controller.stop();
    expect(fixtures.awaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "thread/realtime/stop", threadId: "A" }),
      "thread/realtime/stop", { silent: true });
    expect(useAppStore.getState().draft).toBe("B unsaved draft");
    expect(useAppStore.getState().isStreaming).toBe(false);
  });

  it("retires microphone preparation across A to B to A without launching a stale call", async () => {
    let grant!: () => void;
    fixtures.open.mockImplementationOnce(() => new Promise<void>(resolve => { grant = resolve; }));
    const controller = new RealtimeController();
    const starting = controller.start(capability);
    await waitFor(() => expect(fixtures.open).toHaveBeenCalledOnce());
    useAppStore.setState({ conversationId: "B" });
    useAppStore.setState({ conversationId: "A" });
    grant(); await starting;
    await waitFor(() => expect(controller.getSnapshot().phase).toBe("inactive"));
    expect(fixtures.awaitResult).not.toHaveBeenCalled();
    expect(fixtures.close).toHaveBeenCalledOnce();
  });

  it("keeps the remote owner unsettled after disconnect until the original stop receipt", async () => {
    const controller = new RealtimeController();
    await controller.start(capability); controller.handleEvent(started);
    fixtures.send.mockClear();
    useAppStore.setState({ isConnected: false, conversationId: "B" });
    controller.onTransportClosed();
    fixtures.frames!(frame, .5);
    expect(fixtures.send).not.toHaveBeenCalled();
    expect(controller.getSnapshot()).toMatchObject({ phase: "stopping", conversationId: "A" });
    expect(fixtures.awaitResult).toHaveBeenCalledTimes(1);
    useAppStore.setState({ isConnected: true });
    controller.onTransportReady();
    await waitFor(() => expect(controller.getSnapshot().phase).toBe("inactive"));
    expect(fixtures.awaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "thread/realtime/stop", threadId: "A" }),
      "thread/realtime/stop", { silent: true });
  });

  it("reports real microphone refusal and does not start the host", async () => {
    fixtures.open.mockRejectedValueOnce(new DOMException("Microphone denied", "NotAllowedError"));
    const controller = new RealtimeController();
    await controller.start(capability);
    expect(fixtures.awaitResult).not.toHaveBeenCalled();
    expect(fixtures.toast).toHaveBeenCalledWith("NotAllowedError: Microphone denied", "error");
    expect(controller.getSnapshot().phase).toBe("inactive");
    expect(fixtures.close).toHaveBeenCalledOnce();
  });
});

describe("Source timeline to live and cold frontend projection", () => {
  it("stores A speech while B stays unchanged and projects the same completed item after cold hydration", () => {
    useAppStore.setState({ conversationId: "B", messages: [{ id: "B-message", role: "user", content: "B draft conversation", artifacts: [], timestamp: 1 }] });
    const route = (event: unknown) => {
      const parsed = normalizeInboundServerEvent(event);
      expect(parsed).not.toBeNull();
      expect(handleRealtimeEvent(parsed!)).toBe(true);
    };
    const base = { threadId: "A", conversation_id: "A" };
    route({ ...base, type: "thread/realtime/item/started", item: item("speech-1", "") });
    route({ ...base, type: "thread/realtime/item/transcript/delta", itemId: "speech-1", delta: "Actual spoken answer" });
    route({ ...base, type: "thread/realtime/item/completed", item: item("speech-1", "Actual spoken answer") });
    route({ ...base, type: "thread/realtime/item/transcript/delta", itemId: "speech-1", delta: " stale" });
    route({ ...base, type: "thread/realtime/item/started", item: item("speech-1", "") });
    const live = useAppStore.getState().conversationMessages.A;
    expect(live).toHaveLength(1);
    expect(useAppStore.getState().messages[0].id).toBe("B-message");
    expect(useAppStore.getState().isStreaming).toBe(false);
    const cold = hydrateMessages([{ id: "speech-1", role: "assistant", content: "Actual spoken answer",
      timestamp: live[0].timestamp, blocks: [{ type: "realtime", item: item("speech-1", "Actual spoken answer") }] }]);
    expect(projectMessagesToTurns(cold, false)).toEqual(projectMessagesToTurns(live, false));
    expect(projectMessagesToTurns(live, false)[0].finalAnswerCell?.markdownSource).toBe("Actual spoken answer");
  });

  it("does not turn session facts or promoted existing agent items into extra answer bubbles", () => {
    const facts: RealtimeItem[] = [{ id: "start", realtimeSessionId: "s", type: "realtimeSessionStarted" },
      { id: "promoted", realtimeSessionId: "s", type: "bemItemPromoted", turnId: "turn", itemId: "original", presentation: { type: "wholeItem" } },
      { id: "end", realtimeSessionId: "s", type: "realtimeSessionClosed", outcome: "ended" }];
    facts.forEach(value => handleRealtimeEvent({ type: "thread/realtime/item/completed", threadId: "A", conversation_id: "A", item: value }));
    expect(useAppStore.getState().messages).toHaveLength(3);
    expect(projectMessagesToTurns(useAppStore.getState().messages, false)).toHaveLength(0);
  });

  it("rejects mismatched thread identity and malformed Source payloads before projection", () => {
    expect(normalizeInboundServerEvent({ ...started, threadId: "B" })).toBeNull();
    expect(normalizeInboundServerEvent({ ...started, version: "auto" })).toBeNull();
    expect(normalizeInboundServerEvent({ type: "thread/realtime/outputAudio/delta", threadId: "A", conversation_id: "A",
      audio: { ...frame, sampleRate: 0 } })).toBeNull();
    expect(normalizeInboundServerEvent({ type: "thread/realtime/item/completed", threadId: "A", conversation_id: "A",
      item: { ...item("speech", "body"), role: "system" } })).toBeNull();
  });
});

describe("the proven composer entry point", () => {
  it("replaces the empty send action, and restores it for text or unavailable capability", () => {
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn() } });
    vi.stubGlobal("AudioContext", class {}); vi.stubGlobal("AudioWorkletNode", class {});
    const view = render(<RealtimeComposerActions sendButton={<button>Send existing message</button>} />);
    expect(screen.getByRole("button", { name: "开始语音聊天" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send existing message" })).toBeNull();
    act(() => useAppStore.setState({ draft: "existing text" }));
    expect(screen.getByRole("button", { name: "Send existing message" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "开始语音聊天" })).toBeNull();
    act(() => useAppStore.setState({ draft: "", runtimeCapabilities: { realtime: { ...capability, configured: false } } }));
    expect(screen.getByRole("button", { name: "Send existing message" })).toBeTruthy();
    view.unmount();
    expect(realtimeController.getSnapshot().phase).toBe("inactive");
  });
});

it("encodes actual signed 16-bit little endian microphone samples", async () => {
  const { encodeRealtimePcm } = await vi.importActual<typeof import("./realtime-audio")>("./realtime-audio");
  const encoded = encodeRealtimePcm(new Float32Array([-1, 0, 1, .5, -2, 2]));
  const bytes = Uint8Array.from(atob(encoded), value => value.charCodeAt(0));
  const values = new DataView(bytes.buffer);
  expect(Array.from({ length: 6 }, (_, index) => values.getInt16(index * 2, true))).toEqual([-32768, 0, 32767, 16384, -32768, 32767]);
});
