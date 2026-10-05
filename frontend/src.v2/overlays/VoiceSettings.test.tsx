/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { VoiceSettings } from "./VoiceSettings";
import { useAppStore } from "../stores";
import { defaultWorkbenchPreferences } from "../lib/workbench-preferences";
const supported = { provider: "custom", label: "自定义服务", endpoint: "https://speech.example/v1", compatible: true, configured: true, can_attempt: true, reason: "配置未验证", last_check: null };
let mediaDevices: EventTarget & { enumerateDevices: ReturnType<typeof vi.fn> };
beforeEach(() => {
  mediaDevices = Object.assign(new EventTarget(), { enumerateDevices: vi.fn(async () => []) });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: mediaDevices });
  useAppStore.setState({ workbenchPreferences: { ...defaultWorkbenchPreferences }, currentProvider: "custom", currentProviderBaseUrl: "" });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(supported))));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("keeps service parameters behind configuration and does not silently enable dictation", async () => {
  render(<VoiceSettings />);
  await screen.findByText("尚未启用");
  expect(screen.queryByRole("textbox", { name: "转录模型" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /配置服务/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "转录模型" }), { target: { value: "asr-test" } });
  expect(useAppStore.getState().workbenchPreferences.speechEnabled).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "保存并启用" }));
  await waitFor(() => expect(useAppStore.getState().workbenchPreferences).toMatchObject({ speechEnabled: true, speechModel: "asr-test" }));
  expect(screen.queryByRole("textbox", { name: "转录模型" })).toBeNull();
  expect(vi.mocked(fetch).mock.calls.every(([url]) => String(url).includes("/api/voice/status"))).toBe(true);
});
it("preserves the configuration draft when the selected service does not support dictation", async () => {
  render(<VoiceSettings />);
  await screen.findByText("尚未启用");
  fireEvent.click(screen.getByRole("button", { name: /配置服务/ }));
  fireEvent.change(screen.getByRole("textbox", { name: "转录模型" }), { target: { value: "my-model" } });
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...supported, compatible: false, can_attempt: false, reason: "此服务不能用于听写" })));
  fireEvent.click(screen.getByRole("button", { name: "保存并启用" }));
  expect((await screen.findByRole("alert")).textContent).toContain("此服务不能用于听写");
  expect((screen.getByRole("textbox", { name: "转录模型" }) as HTMLInputElement).value).toBe("my-model");
  expect(useAppStore.getState().workbenchPreferences.speechEnabled).toBe(false);
});

it("retains a selected microphone when absent and refreshes devices on activation and device changes", async () => {
  useAppStore.getState().setWorkbenchPreferences({ microphoneId: "saved-mic" });
  const { rerender } = render(<VoiceSettings active={false} />);
  expect(mediaDevices.enumerateDevices).not.toHaveBeenCalled();
  const trigger = screen.getByRole("button", { name: "麦克风设备，当前：已选设备（当前未检测到）" });
  fireEvent.click(trigger);
  expect(screen.getByRole("option", { name: "已选设备（当前未检测到）" })).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("option", { name: "已选设备（当前未检测到）" }), { key: "Escape" });
  rerender(<VoiceSettings active />);
  await waitFor(() => expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(1));
  mediaDevices.enumerateDevices.mockResolvedValue([{ deviceId: "saved-mic", label: "USB 麦克风", kind: "audioinput" }]);
  act(() => mediaDevices.dispatchEvent(new Event("devicechange")));
  const updatedTrigger = await screen.findByRole("button", { name: "麦克风设备，当前：USB 麦克风" });
  fireEvent.click(updatedTrigger);
  expect(screen.getByRole("option", { name: "USB 麦克风", selected: true })).toBeTruthy();
  expect(screen.queryByRole("option", { name: "已选设备（当前未检测到）" })).toBeNull();
  rerender(<VoiceSettings active={false} />);
  act(() => mediaDevices.dispatchEvent(new Event("devicechange")));
  expect(mediaDevices.enumerateDevices).toHaveBeenCalledTimes(2);
});
