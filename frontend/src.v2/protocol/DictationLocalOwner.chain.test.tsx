/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { defaultWorkbenchPreferences } from "../lib/workbench-preferences";
import { currentDictationPreferences, ensureDesktopDictationPreferences, saveDictationPreferences, consumeDictationProviderSettings } from "../lib/dictation-preferences";
import { useDesktopBehaviorPreferences } from "../hooks/useDesktopBehaviorPreferences";
import { dictationController } from "../composer/dictation-controller";
import { fetchVoiceService, listDictationRecordings, saveDictationRecording, transcribeDictationRecording, downloadDictationRecording, deleteDictationRecording } from "./voice";
import { ProviderTab } from "../overlays/ProviderTab";
import { fetchWithTimeout } from "./api";
import type { DesktopPreferences } from "../desktop/runtime";
import type { LLMSettingsPayload } from "../overlays/settingsShared";

const remoteBase = "http://remote-owner.invalid:19100";
let localRuntime = { apiBaseUrl: "http://127.0.0.1:19200", runtimeToken: "fixture-local-token", revision: 1 };
let preferences: DesktopPreferences;
const preferenceListeners = new Set<(value: DesktopPreferences) => void>();
const bridge = {
  getLocalRuntime: vi.fn(async () => localRuntime),
  migratePreferences: vi.fn(async () => preferences),
  openProviderSettings: vi.fn(async () => true as const),
  consumeProviderSettings: vi.fn(async () => false),
};
const nativePreferences = {
  get: vi.fn(async () => preferences),
  set: vi.fn(async (patch: Partial<DesktopPreferences>) => { preferences = { ...preferences, ...patch }; preferenceListeners.forEach(listener => listener(preferences)); return preferences; }),
  onChanged: (listener: (value: DesktopPreferences) => void) => { preferenceListeners.add(listener); return () => { preferenceListeners.delete(listener); }; },
};
var nativeDesktop = { dictation: bridge, preferences: nativePreferences };
vi.mock("../desktop/runtime", async original => ({
  ...await original<typeof import("../desktop/runtime")>(),
  desktop: () => nativeDesktop, isDesktop: () => true,
  runtime: () => ({ remoteHost: { id: "fixture-remote-host" } }),
  getDesktopPreferences: () => nativePreferences.get(),
  setDesktopPreferences: (patch: Partial<DesktopPreferences>) => nativePreferences.set(patch),
  onDesktopPreferencesChanged: (listener: (value: DesktopPreferences) => void) => { preferenceListeners.add(listener); return () => { preferenceListeners.delete(listener); }; },
}));
vi.mock("./api", async original => ({ ...await original<typeof import("./api")>(), apiBase: () => remoteBase, runtimeToken: () => "fixture-remote-token", fetchWithTimeout: vi.fn((url, options) => fetch(url, options)) }));
vi.mock("./ws-outbox", async original => ({ ...await original<typeof import("./ws-outbox")>(), sendClientCommandAwaitResult: vi.fn(async () => ({ type: "command.result", level: "success" })) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const service = { provider: "custom", label: "Local speech", endpoint: "https://speech.example/v1", compatible: true, configured: true, can_attempt: true, reason: "configured", last_check: null };
const record = { id: "local-recording", created_at: 1, mime_type: "audio/webm", language: "zh", duration_ms: 1000, status: "saved" as const, conversation_id: "remote-chat", workspace_root: "/remote/workspace", text: "", error: "" };
const getUserMedia = vi.fn();
function PreferencesHarness() { useDesktopBehaviorPreferences(); return null; }

beforeEach(() => {
  vi.clearAllMocks(); preferenceListeners.clear(); localStorage.clear();
  preferences = { ...defaultWorkbenchPreferences, speechEnabled: true, speechProvider: "custom", speechModel: "local-asr", dictationDictionary: ["Local exact phrase"],
    defaultTerminalLocation: "bottom", composerPlainTextMode: false, primaryNumberShortcutTarget: "tabs" } as DesktopPreferences;
  localRuntime = { apiBaseUrl: "http://127.0.0.1:19200", runtimeToken: "fixture-local-token", revision: 1 };
  nativePreferences.get.mockImplementation(async () => preferences);
  nativePreferences.set.mockImplementation(async patch => { preferences = { ...preferences, ...patch }; preferenceListeners.forEach(listener => listener(preferences)); return preferences; });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  useAppStore.setState({ conversationId: "remote-chat", workingDirectory: "/remote/workspace", draft: "remote authored draft", settingsOpen: false,
    workbenchPreferences: { ...defaultWorkbenchPreferences, proseSize: 19, dictationDictionary: ["legacy renderer phrase"] } });
  vi.stubGlobal("fetch", vi.fn(async url => {
    const path = new URL(String(url)).pathname;
    return new Response(JSON.stringify(path.endsWith("/status") ? service : path.endsWith("/transcribe") ? { text: "local transcript" } : path.endsWith("/recordings") ? { recordings: [record] } : {}));
  }));
});
afterEach(async () => { await dictationController.settle(); cleanup(); vi.unstubAllGlobals(); });

it("routes every voice action to the current local runtime and token while the window belongs to a different host", async () => {
  await fetchVoiceService("custom", "local-asr");
  localRuntime = { apiBaseUrl: "http://127.0.0.1:19300", runtimeToken: "fixture-local-new-token", revision: 2 };
  await listDictationRecordings();
  await saveDictationRecording(new Blob(["original bytes"], { type: "audio/webm" }), { language: "zh", duration_ms: 1000, status: "saved", conversation_id: "remote-chat", workspace_root: "/remote/workspace" });
  await transcribeDictationRecording(record.id, service, "local-asr", ["Local exact phrase"]);
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:fixture-audio") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  await downloadDictationRecording(record); await deleteDictationRecording(record.id); click.mockRestore();
  const calls = vi.mocked(fetch).mock.calls;
  expect(calls).toHaveLength(6);
  expect(String(calls[0][0])).toContain("127.0.0.1:19200");
  for (const [url, options] of calls.slice(1)) {
    expect(String(url)).toContain("127.0.0.1:19300");
    expect(new Headers(options!.headers).get("X-MiniCode-Token")).toBe("fixture-local-new-token");
    expect(String(url)).not.toContain(remoteBase);
  }
  expect(bridge.getLocalRuntime).toHaveBeenCalledTimes(6);
  expect(useAppStore.getState().draft).toBe("remote authored draft");
});

it("removes partition voice fields only after native migration commits, then reads current preferences without caching the first snapshot", async () => {
  localStorage.setItem("minicode.workbench.preferences", JSON.stringify({ speechEnabled: true, speechProvider: "custom", speechModel: "remote-legacy-asr", speechLanguage: "ja", microphoneId: "legacy-mic", speechVocabulary: "  exact, phrase、with separators  ", proseSize: 23 }));
  await ensureDesktopDictationPreferences();
  expect(bridge.migratePreferences).toHaveBeenCalledWith(expect.objectContaining({ dictationDictionary: ["exact, phrase、with separators"], speechModel: "remote-legacy-asr" }));
  const persisted = JSON.parse(localStorage.getItem("minicode.workbench.preferences")!);
  expect(persisted.proseSize).toBe(23);
  for (const key of ["speechEnabled", "speechProvider", "speechModel", "speechLanguage", "microphoneId", "dictationDictionary", "speechVocabulary"]) expect(persisted).not.toHaveProperty(key);
  expect(useAppStore.getState().workbenchPreferences.speechModel).toBe("local-asr");
  useAppStore.getState().setWorkbenchPreferences({ tabSize: 2 });
  expect(JSON.parse(localStorage.getItem("minicode.workbench.preferences")!)).not.toHaveProperty("dictationDictionary");
  preferences = { ...preferences, speechModel: "later-local-asr" };
  expect((await currentDictationPreferences()).speechModel).toBe("later-local-asr");
});

it("mirrors authoritative device preferences through the existing broadcast independently of renderer storage partitions", async () => {
  await ensureDesktopDictationPreferences(); render(<PreferencesHarness />);
  await waitFor(() => expect(preferenceListeners.size).toBeGreaterThan(0));
  await act(async () => { await nativePreferences.set({ dictationDictionary: ["other-partition phrase"], microphoneId: "shared-local-mic" }); });
  expect(useAppStore.getState().workbenchPreferences).toMatchObject({ dictationDictionary: ["other-partition phrase"], microphoneId: "shared-local-mic" });
  await saveDictationPreferences({ speechLanguage: "ja" });
  expect(nativePreferences.set).toHaveBeenCalledWith({ speechLanguage: "ja" });
  expect(useAppStore.getState().draft).toBe("remote authored draft");
});

it("captures the original owner before asynchronous native preference reads and never starts the microphone after navigation", async () => {
  let resolvePreferences!: (value: DesktopPreferences) => void;
  nativePreferences.get.mockImplementationOnce(() => new Promise(resolve => { resolvePreferences = resolve; }));
  const start = dictationController.start();
  await waitFor(() => expect(resolvePreferences).toBeTypeOf("function"));
  act(() => useAppStore.setState({ conversationId: "later-chat", workingDirectory: "/later/root", draft: "later authored draft" }));
  resolvePreferences(preferences); await start;
  expect(getUserMedia).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("later authored draft");
});

it("opens the real local provider destination from an unconfigured remote composer without replacing its draft", async () => {
  preferences = { ...preferences, speechEnabled: false, speechModel: "" };
  await dictationController.start();
  expect(bridge.openProviderSettings).toHaveBeenCalledOnce(); expect(fetch).not.toHaveBeenCalled(); expect(getUserMedia).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "remote-chat", draft: "remote authored draft", settingsOpen: false });
});

it("saves remote model profiles to their own host without rebinding the device dictation provider", async () => {
  const payload: LLMSettingsPayload = { provider: "custom", active_model: "remote-model", provider_history: [], custom: { display_name: "Remote profile", base_url: "https://remote-provider.invalid/v1", model: "remote-model", available_models: ["remote-model"], wire_api: "chat", auth_header: false } };
  vi.mocked(fetchWithTimeout).mockImplementationOnce(async (_url, options) => { const request = JSON.parse(String(options?.body)); return new Response(JSON.stringify({ provider: "custom", custom: request.custom })); });
  useAppStore.setState({ workbenchPreferences: { ...useAppStore.getState().workbenchPreferences, speechEnabled: true, speechProvider: "custom", speechModel: "device-model" } });
  render(<ProviderTab selectedProvider="custom" settingsPayload={payload} settingsPayloadRef={{ current: payload }} onProviderChange={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "编辑 Remote profile" })); fireEvent.click(screen.getByText("高级设置"));
  expect(screen.queryByRole("textbox", { name: "转录模型" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  await waitFor(() => expect(fetchWithTimeout).toHaveBeenCalledOnce());
  expect(String(vi.mocked(fetchWithTimeout).mock.calls[0][0])).toBe(remoteBase + "/api/llm/settings");
  expect(nativePreferences.set).not.toHaveBeenCalled();
  expect(useAppStore.getState().workbenchPreferences.speechModel).toBe("device-model");
});


it("consumes a retained local provider intent after readiness exactly once without changing the current chat or draft", async () => {
  bridge.consumeProviderSettings.mockResolvedValueOnce(false);
  await consumeDictationProviderSettings();
  expect(useAppStore.getState().settingsOpen).toBe(false);
  bridge.consumeProviderSettings.mockResolvedValueOnce(true).mockResolvedValueOnce(false);
  await consumeDictationProviderSettings();
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "provider", conversationId: "remote-chat", draft: "remote authored draft" });
  useAppStore.getState().setSettingsTab("voice");
  await consumeDictationProviderSettings();
  expect(useAppStore.getState().settingsTab).toBe("voice");
});
