/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VoiceSettings } from "./VoiceSettings";
import { useAppStore } from "../stores";
import { defaultWorkbenchPreferences } from "../lib/workbench-preferences";
import type { DictationHotkeyState } from "../desktop/runtime";
const supported = { provider: "custom", label: "自定义服务", endpoint: "https://speech.example/v1", compatible: true, configured: true, can_attempt: true, reason: "配置未验证", last_check: null };
const recording = { id: "original-audio-id", created_at: 1792430000, mime_type: "audio/webm", language: "zh", duration_ms: 1250, status: "failed", conversation_id: "original-owner", workspace_root: "C:/original", text: "saved transcript", error: "actual earlier provider error" };
let recordings = [recording];
function nativeBridge() {
  let state: DictationHotkeyState = { available: true, holdHotkey: "Ctrl+Shift+Space", toggleHotkey: "", error: "" };
  return { getLocalRuntime: vi.fn(async () => ({ apiBaseUrl: "http://127.0.0.1:8000", runtimeToken: "local", revision: 1 })), get: vi.fn(async () => state), onState: vi.fn(() => () => {}), suspend: vi.fn(async () => {}),
    configure: vi.fn(async (patch: Partial<DictationHotkeyState>) => { state = { ...state, ...patch }; return state; }) };
}
var bridge: ReturnType<typeof nativeBridge> | undefined;
vi.mock("../desktop/runtime", async original => ({ ...await original<typeof import("../desktop/runtime")>(), desktop: () => bridge ? { dictation: bridge } : undefined }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
let mediaDevices: EventTarget & { enumerateDevices: ReturnType<typeof vi.fn>; getUserMedia: ReturnType<typeof vi.fn> };
let stopMicrophone: ReturnType<typeof vi.fn>;
beforeEach(() => {
  bridge = undefined;
  recordings = [recording];
  stopMicrophone = vi.fn();
  mediaDevices = Object.assign(new EventTarget(), { enumerateDevices: vi.fn(async () => []),
    getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: stopMicrophone }] })) });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: mediaDevices });
  useAppStore.setState({ workbenchPreferences: { ...defaultWorkbenchPreferences, speechEnabled: true, speechModel: "asr-current", dictationDictionary: ["MiniCode", "术语,保留"] }, currentProvider: "custom", currentProviderBaseUrl: "", draft: "keep current draft" });
  vi.stubGlobal("fetch", vi.fn(async url => new Response(JSON.stringify(String(url).includes("/status") ? supported : String(url).includes("/transcribe") ? { text: "retried original recording" } : { recordings }))));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("uses Source voice controls without custom service diagnostics or unavailable global shortcuts", async () => {
  render(<VoiceSettings />); await screen.findByText("saved transcript");
  for (const name of ["检测服务", "配置服务", "管理服务商", "重新读取语音配置", "刷新麦克风"]) expect(screen.queryByRole("button", { name })).toBeNull();
  expect(screen.queryByRole("textbox", { name: "转录模型" })).toBeNull();
  expect(screen.queryByRole("switch", { name: "启用听写" })).toBeNull();
  expect(screen.queryByRole("button", { name: "听写快捷键" })).toBeNull();
  expect(screen.getByText("最近 20 段录音保存在这台设备上。")).toBeTruthy();
  expect(vi.mocked(fetch).mock.calls.every(([url]) => !String(url).includes("/check"))).toBe(true);
});

it("retries the selected stored ID with current provider and dictionary without writing or sending any draft", async () => {
  recordings = [{ ...recording, text: "" }];
  render(<VoiceSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "重新转录" }));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes("original-audio-id/transcribe"))).toBe(true));
  const request = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes("/transcribe"))!;
  expect(JSON.parse(String(request[1]!.body))).toEqual({ provider: "custom", expected_endpoint: supported.endpoint, model: "asr-current", dictionary: ["MiniCode", "术语,保留"] });
  expect(useAppStore.getState().draft).toBe("keep current draft");
});

it("copies only the selected transcript and deletes only its saved ID", async () => {
  const writeText = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<VoiceSettings />); await screen.findByText("saved transcript");
  expect(screen.queryByRole("button", { name: "重新转录" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "复制转录文字" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("saved transcript"));
  await waitFor(() => expect((screen.getByRole("button", { name: "录音操作" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "录音操作" }));
  expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["下载录音", "删除"]);
  fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url, options]) => String(url).endsWith("/original-audio-id") && options?.method === "DELETE")).toBe(true));
});

it("shows the actual audio-only recording status and keeps retries unavailable without dictation", async () => {
  recordings = [
    { ...recording, id: "cancelled-audio", status: "cancelled", text: "", error: "" },
    { ...recording, id: "interrupted-audio", status: "interrupted", text: "", error: "interrupted provider request" },
    { ...recording, id: "saved-audio", status: "saved", text: "", error: "" },
  ];
  useAppStore.getState().setWorkbenchPreferences({ speechEnabled: false });
  render(<VoiceSettings />);
  await screen.findByText("录音已取消");
  expect(screen.getByText("录音已中断")).toBeTruthy();
  expect(screen.getByText("录音已保存")).toBeTruthy();
  expect(screen.getByText("interrupted provider request")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "复制转录文字" })).toBeNull();
  expect(screen.queryByRole("button", { name: "重新转录" })).toBeNull();
});

it("edits, adds, trims, and removes individual dictionary entries in the one persisted array", async () => {
  render(<VoiceSettings />);
  const first = screen.getByRole("textbox", { name: "词典条目 1" });
  fireEvent.change(first, { target: { value: "  Kubernetes  " } }); fireEvent.blur(first);
  expect(useAppStore.getState().workbenchPreferences.dictationDictionary).toEqual(["Kubernetes", "术语,保留"]);
  fireEvent.click(screen.getByRole("button", { name: "添加条目" }));
  const third = screen.getByRole("textbox", { name: "词典条目 3" });
  fireEvent.change(third, { target: { value: "中文项目" } }); fireEvent.blur(third);
  expect(useAppStore.getState().workbenchPreferences.dictationDictionary).toEqual(["Kubernetes", "术语,保留", "中文项目"]);
  fireEvent.click(screen.getByRole("button", { name: "移除词典条目 2" }));
  expect(useAppStore.getState().workbenchPreferences.dictationDictionary).toEqual(["Kubernetes", "中文项目"]);
  expect(JSON.parse(localStorage.getItem("minicode.workbench.preferences")!)).not.toHaveProperty("speechVocabulary");
});

it("retains a disconnected selected microphone and refreshes from actual devicechange events", async () => {
  useAppStore.getState().setWorkbenchPreferences({ microphoneId: "saved-mic" });
  const { rerender } = render(<VoiceSettings active={false} />);
  expect(mediaDevices.enumerateDevices).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "麦克风设备，当前：已选择的麦克风" })).toHaveProperty("disabled", true);
  rerender(<VoiceSettings active />); await waitFor(() => expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(1));
  mediaDevices.enumerateDevices.mockResolvedValue([{ deviceId: "other-mic", label: "Other microphone", kind: "audioinput" }]);
  act(() => mediaDevices.dispatchEvent(new Event("devicechange")));
  fireEvent.click(await screen.findByRole("button", { name: "麦克风设备，当前：不可用的麦克风" }));
  await screen.findByRole("option", { name: "Other microphone" });
  expect(screen.queryByRole("option", { name: "不可用的麦克风" })).toBeNull();
  expect(useAppStore.getState().workbenchPreferences.microphoneId).toBe("saved-mic");
  fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
  mediaDevices.enumerateDevices.mockResolvedValue([{ deviceId: "saved-mic", label: "USB 麦克风", kind: "audioinput" }]);
  act(() => mediaDevices.dispatchEvent(new Event("devicechange")));
  expect(await screen.findByRole("button", { name: "麦克风设备，当前：USB 麦克风" })).toBeTruthy();
  expect(mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
});

it("requests microphone access only on a real open, keeps loading through the device read and releases the stream", async () => {
  let allow!: (stream: { getTracks: () => { stop: typeof stopMicrophone }[] }) => void;
  let enumerate!: (devices: { deviceId: string; label: string; kind: string }[]) => void;
  mediaDevices.getUserMedia.mockReturnValueOnce(new Promise(resolve => { allow = resolve; }));
  mediaDevices.enumerateDevices.mockResolvedValueOnce([]).mockReturnValueOnce(new Promise(resolve => { enumerate = resolve; }));
  render(<VoiceSettings />);
  await waitFor(() => expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(1));
  expect(mediaDevices.getUserMedia).not.toHaveBeenCalled();
  const trigger = screen.getByRole("button", { name: "麦克风设备，当前：系统默认" });
  fireEvent(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger, { detail: 1 });
  expect(screen.getByRole("listbox").style.width).toBe("240px");
  expect(mediaDevices.getUserMedia).toHaveBeenCalledExactlyOnceWith({ audio: true });
  expect(screen.getByRole("status").textContent).toContain("正在加载麦克风");
  expect(screen.queryByText("未找到麦克风")).toBeNull();
  await act(async () => allow({ getTracks: () => [{ stop: stopMicrophone }] }));
  await waitFor(() => expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(2));
  expect(stopMicrophone).not.toHaveBeenCalled();
  expect(screen.getByRole("status").textContent).toContain("正在加载麦克风");
  await act(async () => enumerate([
    { deviceId: "default", label: "Default alias", kind: "audioinput" },
    { deviceId: "", label: "Unexposed device", kind: "audioinput" },
    { deviceId: "usb", label: "USB microphone", kind: "audioinput" },
    { deviceId: "camera", label: "Camera", kind: "videoinput" },
  ]));
  expect(screen.getAllByRole("option").map(option => option.textContent)).toEqual(["系统默认", "USB microphone"]);
  expect(screen.getByRole("separator")).toBeTruthy();
  expect(screen.queryByRole("status")).toBeNull();
  expect(stopMicrophone).toHaveBeenCalledTimes(1);
  expect(useAppStore.getState().draft).toBe("keep current draft");
});

it("keeps a real permission denial in the menu while refetching actual devices", async () => {
  mediaDevices.getUserMedia.mockRejectedValueOnce(new DOMException("Microphone permission denied", "NotAllowedError"));
  mediaDevices.enumerateDevices.mockResolvedValue([{ deviceId: "known", label: "Known microphone", kind: "audioinput" }]);
  render(<VoiceSettings />);
  await waitFor(() => expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(1));
  const trigger = screen.getByRole("button", { name: "麦克风设备，当前：系统默认" });
  fireEvent.click(trigger);
  expect((await screen.findByRole("alert")).textContent).toContain("Microphone permission denied");
  expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("option", { name: "Known microphone" })).toBeTruthy();
  expect(screen.queryByText("未找到麦克风")).toBeNull();
  expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  expect(stopMicrophone).not.toHaveBeenCalled();
});

it("retries actual enumeration failures by reopening and stops granted streams on both outcomes", async () => {
  mediaDevices.enumerateDevices.mockResolvedValueOnce([]).mockRejectedValueOnce(new Error("Device enumeration failed"));
  render(<VoiceSettings />);
  await waitFor(() => expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(1));
  const trigger = screen.getByRole("button", { name: "麦克风设备，当前：系统默认" });
  fireEvent.click(trigger);
  expect((await screen.findByRole("alert")).textContent).toContain("Device enumeration failed");
  expect(stopMicrophone).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
  fireEvent.click(trigger);
  await waitFor(() => expect(screen.getByRole("status").textContent).toContain("未找到麦克风"));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getAllByRole("option")).toHaveLength(1);
  expect(stopMicrophone).toHaveBeenCalledTimes(2);
});

it("closes the microphone menu when hidden and still releases an already requested stream", async () => {
  let allow!: (stream: { getTracks: () => { stop: typeof stopMicrophone }[] }) => void;
  mediaDevices.getUserMedia.mockReturnValueOnce(new Promise(resolve => { allow = resolve; }));
  const view = render(<VoiceSettings />);
  await waitFor(() => expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "麦克风设备，当前：系统默认" }));
  view.rerender(<VoiceSettings active={false} />);
  expect(screen.queryByRole("listbox")).toBeNull();
  await act(async () => allow({ getTracks: () => [{ stop: stopMicrophone }] }));
  expect(stopMicrophone).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
});

it("disables microphone selection when the actual device API is unavailable", () => {
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
  render(<VoiceSettings />);
  const trigger = screen.getByRole("button", { name: "麦克风设备，当前：系统默认" });
  expect(trigger).toHaveProperty("disabled", true);
  expect(screen.getByText("当前设备不支持选择麦克风。")).toBeTruthy();
  fireEvent.click(trigger);
  expect(screen.queryByRole("listbox")).toBeNull();
});

it("captures native hold and optional single-tap settings only when the actual bridge is available", async () => {
  bridge = nativeBridge();
  render(<VoiceSettings />); await screen.findByRole("button", { name: "听写快捷键" });
  expect(screen.queryByRole("button", { name: "单击听写快捷键" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "高级" }));
  fireEvent.click(screen.getByRole("button", { name: "单击听写快捷键" }));
  fireEvent.keyDown(window, { key: "F12", code: "F12", altKey: true });
  await waitFor(() => expect(bridge!.configure).toHaveBeenCalledWith({ toggleHotkey: "Alt+F12" }));
  expect(bridge.suspend).toHaveBeenCalledWith(true);
  await waitFor(() => expect(bridge!.suspend).toHaveBeenCalledWith(false));
  expect(screen.getByRole("button", { name: "单击听写快捷键" }).textContent).toBe("Alt+F12");
});

it("keeps the successful native binding when registration fails and releases capture when hidden", async () => {
  bridge = nativeBridge(); bridge.configure.mockRejectedValueOnce(new Error("fixture native shortcut conflict"));
  const { rerender } = render(<VoiceSettings />); await screen.findByRole("button", { name: "听写快捷键" });
  fireEvent.click(screen.getByRole("button", { name: "听写快捷键" })); fireEvent.keyDown(window, { key: "F11", code: "F11", altKey: true });
  expect((await screen.findByRole("alert")).textContent).toContain("fixture native shortcut conflict");
  expect(screen.getByRole("button", { name: "听写快捷键" }).textContent).toBe("Ctrl+Shift+Space");
  fireEvent.click(screen.getByRole("button", { name: "听写快捷键" })); rerender(<VoiceSettings active={false} />);
  fireEvent.keyDown(window, { key: "F10", code: "F10", altKey: true });
  expect(bridge.configure).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(bridge!.suspend).toHaveBeenCalledWith(false));
});

it("invalidates an observed service state after provider persistence even when provider and endpoint strings did not change", async () => {
  render(<VoiceSettings />);
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/status"))).toHaveLength(1));
  act(() => window.dispatchEvent(new Event("dictation:service-changed")));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/status"))).toHaveLength(2));
});


it("reads the same local recording list again when the active voice page regains focus", async () => {
  const { rerender } = render(<VoiceSettings />);
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/recordings"))).toHaveLength(1));
  act(() => window.dispatchEvent(new Event("focus")));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/recordings"))).toHaveLength(2));
  rerender(<VoiceSettings active={false} />);
  act(() => window.dispatchEvent(new Event("focus")));
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith("/recordings"))).toHaveLength(2);
});
