/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { VoiceInput } from "./VoiceInput";
import { defaultWorkbenchPreferences } from "../lib/workbench-preferences";
const stopTrack = vi.fn();
const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const available = { provider: "custom", label: "Test speech", endpoint: "https://speech.example/v1", compatible: true, configured: true, can_attempt: true, reason: "尚未验证", last_check: null };

beforeEach(() => {
  vi.clearAllMocks();
  class Recorder {
    state = "inactive"; mimeType = "audio/webm"; ondataavailable?: (event: { data: Blob }) => void; onstop?: () => void;
    start() { this.state = "recording"; }
    stop() { this.state = "inactive"; this.ondataavailable?.({ data: new Blob(["audio"]) }); this.onstop?.(); }
  }
  class Meter {
    createAnalyser() { return { fftSize: 256, getByteTimeDomainData: (data: Uint8Array) => data.fill(128) }; }
    createMediaStreamSource() { return { connect() {} }; }
    async resume() {}
    async close() {}
  }
  vi.stubGlobal("MediaRecorder", Recorder); vi.stubGlobal("AudioContext", Meter);
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
  vi.stubGlobal("fetch", vi.fn(async (url) => new Response(JSON.stringify(String(url).includes("/status") ? available : { text: "语音草稿" }), { status: 200 })));
  useAppStore.setState({ conversationId: "conv_voice_test", draft: "原有内容", settingsOpen: false,
    workbenchPreferences: { ...defaultWorkbenchPreferences, speechEnabled: true, speechModel: "asr-test" } });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("checks configuration before capture and inserts transcription into the editable draft", async () => {
  render(<VoiceInput />);
  fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  fireEvent.click(await screen.findByRole("button", { name: "结束听写并转为草稿" }));
  await waitFor(() => expect(useAppStore.getState().draft).toBe("原有内容\n语音草稿"));
  expect(stopTrack).toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain("/api/voice/status");
  const form = vi.mocked(fetch).mock.calls[1][1]!.body as FormData;
  expect(form.get("provider")).toBe("custom");
  expect(form.get("expected_endpoint")).toBe("https://speech.example/v1");
});

it("opens the voice settings without microphone access when dictation has not been configured", () => {
  useAppStore.setState({ workbenchPreferences: { ...defaultWorkbenchPreferences } });
  render(<VoiceInput />);
  fireEvent.click(screen.getByRole("button", { name: "配置听写" }));
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, settingsTab: "voice" });
  expect(getUserMedia).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
});

it("does not record with an incompatible service", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...available, compatible: false, can_attempt: false, reason: "Messages 服务不支持听写" })));
  render(<VoiceInput />);
  fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("voice"));
  expect(getUserMedia).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("discards a recording on a conversation change without uploading its audio", async () => {
  render(<VoiceInput />);
  fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  await screen.findByRole("button", { name: "结束听写并转为草稿" });
  act(() => useAppStore.setState({ conversationId: "conv_other_voice" }));
  await waitFor(() => expect(stopTrack).toHaveBeenCalled());
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(useAppStore.getState().draft).toBe("原有内容");
});

it.each([
  { speechProvider: "custom" },
  { speechModel: "another-asr" },
  { microphoneId: "another-microphone" },
  { speechEnabled: false },
])("releases capture without uploading when dictation preferences change: %j", async (change) => {
  render(<VoiceInput />);
  fireEvent.click(screen.getByRole("button", { name: "开始听写" }));
  await screen.findByRole("button", { name: "结束听写并转为草稿" });
  act(() => useAppStore.getState().setWorkbenchPreferences(change));
  await waitFor(() => expect(stopTrack).toHaveBeenCalled());
  expect(screen.queryByRole("meter")).toBeNull();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(useAppStore.getState().draft).toBe("原有内容");
});
