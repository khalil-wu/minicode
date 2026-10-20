/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { VoiceInput } from "./VoiceInput";
import { dictationController } from "./dictation-controller";
import { defaultWorkbenchPreferences, migrateWorkbenchPreferences } from "../lib/workbench-preferences";
import { useNativeDictation } from "../hooks/useNativeDictation";
import type { NativeDictationEvent } from "../desktop/runtime";
import { composerWindowOwnerId } from "../stores/composer-drafts";
let nativeEvent: ((event: NativeDictationEvent) => void) | undefined;
let nativeBridge: { get: ReturnType<typeof vi.fn>; complete: ReturnType<typeof vi.fn>; insert: ReturnType<typeof vi.fn>; onEvent: (listener: (event: NativeDictationEvent) => void) => () => void } | undefined;
vi.mock("../desktop/runtime", async original => ({ ...await original<typeof import("../desktop/runtime")>(), desktop: () => nativeBridge ? {
  dictation: { ...nativeBridge, getLocalRuntime: async () => ({ apiBaseUrl: "http://127.0.0.1:8000", runtimeToken: "fixture-local", revision: 1 }),
    migratePreferences: async () => useAppStore.getState().workbenchPreferences, openProviderSettings: async () => true },
  preferences: { get: async () => useAppStore.getState().workbenchPreferences },
} : undefined }));
function NativeHarness() { useNativeDictation(); return null; }
const stopTrack = vi.fn();
const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] }));
const recorders: Array<{ state: string; stop: () => void; onerror?: () => void }> = [];
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const available = { provider: "custom", label: "Test speech", endpoint: "https://speech.example/v1", compatible: true, configured: true, can_attempt: true, reason: "尚未验证", last_check: null };
const stored = { id: "actual-recording-id", created_at: 1, mime_type: "audio/webm", language: "zh", duration_ms: 1000, status: "saved", conversation_id: "conv_voice_test", workspace_root: "C:/voice", text: "", error: "" };
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
const nativeFetch = async (url: RequestInfo | URL) => json(String(url).includes("/status") ? available : String(url).includes("/transcribe") ? { text: "语音草稿" } : stored);

beforeEach(() => {
  vi.clearAllMocks(); recorders.length = 0; nativeBridge = undefined; nativeEvent = undefined;
  class Recorder {
    state = "inactive"; mimeType = "audio/webm"; ondataavailable?: (event: { data: Blob }) => void; onstop?: () => void; onerror?: () => void;
    constructor() { recorders.push(this); }
    start() { this.state = "recording"; }
    stop() { this.state = "inactive"; this.ondataavailable?.({ data: new Blob(["original-audio"]) }); this.onstop?.(); }
  }
  class Meter {
    createAnalyser() { return { fftSize: 256, getByteTimeDomainData: (data: Uint8Array) => data.fill(128) }; }
    createMediaStreamSource() { return { connect() {} }; }
    async resume() {}
    async close() {}
  }
  vi.stubGlobal("MediaRecorder", Recorder); vi.stubGlobal("AudioContext", Meter);
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  vi.stubGlobal("fetch", vi.fn(nativeFetch));
  useAppStore.setState({ conversationId: "conv_voice_test", workingDirectory: "C:/voice", draft: "原有内容", settingsOpen: false, sideChats: {}, activeSideChatId: null,
    workbenchPreferences: { ...defaultWorkbenchPreferences, speechEnabled: true, speechModel: "asr-test", dictationDictionary: ["MiniCode", "词组,保留原样"] } });
});

afterEach(async () => { await act(() => dictationController.settle()); cleanup(); vi.unstubAllGlobals(); });

it("saves the actual audio before transcribing its ID and appending only the initiating owner's draft", async () => {
  render(<VoiceInput />);
  fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  fireEvent.click(await screen.findByRole("button", { name: "结束听写并转为草稿" }));
  await waitFor(() => expect(useAppStore.getState().draft).toBe("原有内容\n语音草稿"));
  expect(stopTrack).toHaveBeenCalled(); expect(fetch).toHaveBeenCalledTimes(3);
  const calls = vi.mocked(fetch).mock.calls;
  expect(String(calls[0][0])).toContain("/api/voice/status");
  expect(String(calls[1][0])).toContain("/api/voice/recordings");
  const form = calls[1][1]!.body as FormData;
  expect(form.get("conversation_id")).toBe("conv_voice_test"); expect(form.get("workspace_root")).toBe("C:/voice");
  expect((form.get("audio") as Blob).size).toBe("original-audio".length);
  expect(String(calls[2][0])).toContain("actual-recording-id/transcribe");
  expect(JSON.parse(String(calls[2][1]!.body))).toMatchObject({ provider: "custom", expected_endpoint: available.endpoint, dictionary: ["MiniCode", "词组,保留原样"] });
});

it("opens the existing provider editor for service configuration without starting capture", async () => {
  useAppStore.setState({ workbenchPreferences: { ...defaultWorkbenchPreferences } });
  render(<VoiceInput />); fireEvent.click(screen.getByRole("button", { name: "配置听写" }));
  await waitFor(() => expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "provider" }));
  expect(getUserMedia).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
});

it("does not capture audio for an incompatible service", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(json({ ...available, compatible: false, can_attempt: false, reason: "Messages 服务不支持听写" }));
  render(<VoiceInput />); fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("provider"));
  expect(getUserMedia).not.toHaveBeenCalled(); expect(fetch).toHaveBeenCalledTimes(1);
});

it.each([
  { conversationId: "conv_other_voice", draft: "另一个聊天的草稿" },
  { workingDirectory: "C:/other-workspace", draft: "隐藏工作区的草稿" },
])("retains interrupted audio on owner navigation without sending it to a provider: %j", async patch => {
  render(<VoiceInput />); fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  await screen.findByRole("button", { name: "结束听写并转为草稿" });
  act(() => useAppStore.setState(patch));
  await waitFor(() => expect(dictationController.getSnapshot().phase).toBe("idle"));
  const form = vi.mocked(fetch).mock.calls[1][1]!.body as FormData;
  expect(form.get("status")).toBe("interrupted"); expect(form.get("conversation_id")).toBe("conv_voice_test");
  expect(fetch).toHaveBeenCalledTimes(2); expect(useAppStore.getState().draft).toBe(patch.draft);
});

it.each([{ speechProvider: "custom" }, { speechModel: "another-asr" }, { microphoneId: "another-microphone" }, { speechEnabled: false }])(
  "releases capture and retains real audio when routing preferences change: %j", async change => {
  render(<VoiceInput />); fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  await screen.findByRole("button", { name: "结束听写并转为草稿" });
  act(() => useAppStore.getState().setWorkbenchPreferences(change));
  await waitFor(() => expect(dictationController.getSnapshot().phase).toBe("idle"));
  expect(stopTrack).toHaveBeenCalled(); expect(screen.queryByRole("meter")).toBeNull(); expect(fetch).toHaveBeenCalledTimes(2);
  expect((vi.mocked(fetch).mock.calls[1][1]!.body as FormData).get("status")).toBe("interrupted");
});

it("retains cancelled recordings without transcription or draft insertion", async () => {
  render(<VoiceInput />); fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  fireEvent.click(await screen.findByRole("button", { name: "放弃录音" }));
  await waitFor(() => expect(dictationController.getSnapshot().phase).toBe("idle"));
  expect((vi.mocked(fetch).mock.calls[1][1]!.body as FormData).get("status")).toBe("cancelled");
  expect(fetch).toHaveBeenCalledTimes(2); expect(useAppStore.getState().draft).toBe("原有内容");
});

it("does not inject a late transcript into a newly selected conversation", async () => {
  let complete!: (value: Response) => void;
  vi.mocked(fetch).mockImplementation(async url => String(url).includes("/transcribe") ? new Promise(resolve => { complete = resolve; }) : nativeFetch(url));
  render(<VoiceInput />); fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  fireEvent.click(await screen.findByRole("button", { name: "结束听写并转为草稿" }));
  await waitFor(() => expect(complete).toBeTypeOf("function"));
  act(() => useAppStore.setState({ conversationId: "new-chat", draft: "keep new draft" }));
  await act(async () => { complete(json({ text: "late transcript" })); });
  expect(useAppStore.getState().draft).toBe("keep new draft");
  expect(vi.mocked(fetch).mock.calls[2][1]!.signal!.aborted).toBe(true);
});

it("keeps the same captured audio available for a failed-save retry", async () => {
  let failSave = true;
  vi.mocked(fetch).mockImplementation(async url => {
    if (String(url).endsWith("/recordings") && failSave) { failSave = false; throw new Error("fixture local persistence unavailable"); }
    return nativeFetch(url);
  });
  render(<VoiceInput />); fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  fireEvent.click(await screen.findByRole("button", { name: "结束听写并转为草稿" }));
  fireEvent.click(await screen.findByRole("button", { name: "重试保存录音" }));
  await waitFor(() => expect(useAppStore.getState().draft).toBe("原有内容\n语音草稿"));
  expect(recorders).toHaveLength(1);
  const uploads = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/recordings"));
  expect((uploads[0][1]!.body as FormData).get("duration_ms")).toBe((uploads[1][1]!.body as FormData).get("duration_ms"));
});

it("native side-chat ownership appends to that side draft while preserving the main draft", async () => {
  useAppStore.getState().ensureSideChat("side-owner", { workspaceRoot: "C:/voice", permissionMode: "auto" });
  useAppStore.setState({ activeSideChatId: "side-owner" }); useAppStore.getState().setSideChatDraft("side-owner", "侧聊原稿");
  await act(() => dictationController.start({ kind: "side", id: "side-owner", workspaceRoot: "C:/voice", windowOwnerId: composerWindowOwnerId() }));
  act(() => dictationController.stop());
  await waitFor(() => expect(useAppStore.getState().sideChats["side-owner"].draft).toBe("侧聊原稿\n语音草稿"));
  expect(useAppStore.getState().draft).toBe("原有内容");
});

it("the unload barrier waits for actual audio storage without waiting for remote transcription", async () => {
  let saved!: (value: Response) => void;
  vi.mocked(fetch).mockImplementation(async url => String(url).endsWith("/recordings") ? new Promise(resolve => { saved = resolve; }) : nativeFetch(url));
  await act(() => dictationController.start());
  let settled = false;
  const closing = dictationController.settle().then(() => { settled = true; });
  await waitFor(() => expect(saved).toBeTypeOf("function")); expect(settled).toBe(false);
  await act(async () => { saved(json(stored)); await closing; });
  expect(settled).toBe(true); expect(fetch).toHaveBeenCalledTimes(2);
});

it("migrates vocabulary once as one intact entry and uses only the dictionary array afterward", () => {
  const migrated = migrateWorkbenchPreferences({ speechVocabulary: "  MiniCode, TypeScript、项目名称  ", proseSize: 21 });
  expect(migrated.dictationDictionary).toEqual(["MiniCode, TypeScript、项目名称"]);
  expect(migrated).not.toHaveProperty("speechVocabulary"); expect(migrated.proseSize).toBe(21);
  expect(migrateWorkbenchPreferences({ ...migrated, dictationDictionary: [], speechVocabulary: "obsolete" }).dictationDictionary).toEqual([]);
});

it("updates the shared dictionary and device cache from the latest other-window preferences without touching authored drafts", () => {
  const next = { ...useAppStore.getState().workbenchPreferences, dictationDictionary: ["other-window phrase"], microphoneId: "other-window-device" };
  localStorage.setItem("minicode.workbench.preferences", JSON.stringify(next));
  act(() => window.dispatchEvent(new StorageEvent("storage", { key: "minicode.workbench.preferences", storageArea: localStorage })));
  expect(useAppStore.getState().workbenchPreferences).toMatchObject({ dictationDictionary: ["other-window phrase"], microphoneId: "other-window-device" });
  expect(useAppStore.getState().draft).toBe("原有内容");
});

it("native external events use the shared recorder and original insertion token, leaving all drafts editable", async () => {
  nativeBridge = { get: vi.fn(async () => ({ available: true })), complete: vi.fn(async () => {}), insert: vi.fn(async () => true),
    onEvent: listener => { nativeEvent = listener; return () => { nativeEvent = undefined; }; } };
  render(<NativeHarness />);
  act(() => nativeEvent!({ action: "start", token: "native-owner-token", targetKind: "external" }));
  await waitFor(() => expect(dictationController.getSnapshot().phase).toBe("recording"));
  act(() => nativeEvent!({ action: "stop", token: "native-owner-token", targetKind: "external" }));
  await waitFor(() => expect(nativeBridge!.insert).toHaveBeenCalledWith("native-owner-token", "语音草稿"));
  expect(nativeBridge.complete).toHaveBeenCalledWith("native-owner-token"); expect(recorders).toHaveLength(1);
  expect(useAppStore.getState().draft).toBe("原有内容");
});

it("an app settings input target that changes is retained in history without injecting into the primary draft", async () => {
  const input = document.createElement("input"); document.body.append(input); input.focus();
  nativeBridge = { get: vi.fn(async () => ({ available: true })), complete: vi.fn(async () => {}), insert: vi.fn(async () => true),
    onEvent: listener => { nativeEvent = listener; return () => { nativeEvent = undefined; }; } };
  render(<NativeHarness />);
  act(() => nativeEvent!({ action: "start", token: "app-input-token", targetKind: "composer" }));
  await waitFor(() => expect(dictationController.getSnapshot().phase).toBe("recording"));
  input.remove();
  act(() => nativeEvent!({ action: "stop", token: "app-input-token", targetKind: "composer" }));
  await waitFor(() => expect(nativeBridge!.complete).toHaveBeenCalledWith("app-input-token"));
  expect(nativeBridge.insert).not.toHaveBeenCalled(); expect(useAppStore.getState().draft).toBe("原有内容");
  expect(String(vi.mocked(fetch).mock.calls[2][0])).toContain("actual-recording-id/transcribe");
});



it("an unchanged app settings input consumes the original native session without writing the primary chat", async () => {
  const input = document.createElement("input"); document.body.append(input); input.focus();
  nativeBridge = { get: vi.fn(async () => ({ available: true })), complete: vi.fn(async () => {}), insert: vi.fn(async () => true),
    onEvent: listener => { nativeEvent = listener; return () => { nativeEvent = undefined; }; } };
  render(<NativeHarness />);
  act(() => nativeEvent!({ action: "start", token: "unchanged-app-input", targetKind: "composer" }));
  await waitFor(() => expect(dictationController.getSnapshot().phase).toBe("recording"));
  act(() => nativeEvent!({ action: "stop", token: "unchanged-app-input", targetKind: "composer" }));
  await waitFor(() => expect(nativeBridge!.insert).toHaveBeenCalledWith("unchanged-app-input", "语音草稿"));
  expect(document.activeElement).toBe(input); expect(useAppStore.getState().draft).toBe("原有内容"); input.remove();
});

it("does not assign an unknown side-chat workspace to the currently active primary owner", async () => {
  useAppStore.getState().ensureSideChat("unknown-root-side", { workspaceRoot: "C:/original-side", permissionMode: "auto" });
  const { workspaceRoot: _removed, ...unknown } = useAppStore.getState().sideChats["unknown-root-side"];
  useAppStore.setState({ activeSideChatId: "unknown-root-side", sideChats: { "unknown-root-side": unknown } });
  const container = document.createElement("div"); container.className = "composer-container"; container.dataset.dictationSideChat = "unknown-root-side";
  const input = document.createElement("textarea"); container.append(input); document.body.append(container); input.focus();
  await act(() => dictationController.start());
  expect(getUserMedia).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("原有内容"); container.remove();
});

it("retains audio rather than transcribing with changed provider credentials at the same endpoint", async () => {
  await act(() => dictationController.start());
  act(() => window.dispatchEvent(new CustomEvent("dictation:service-changed", { detail: { provider: "custom" } })));
  await waitFor(() => expect(dictationController.getSnapshot().phase).toBe("idle"));
  expect(fetch).toHaveBeenCalledTimes(2);
  expect((vi.mocked(fetch).mock.calls[1][1]!.body as FormData).get("status")).toBe("interrupted");
  expect(useAppStore.getState().draft).toBe("原有内容");
});
