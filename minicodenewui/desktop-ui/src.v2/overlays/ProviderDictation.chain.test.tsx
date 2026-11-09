/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ProviderTab } from "./ProviderTab";
import { useAppStore } from "../stores";
import { defaultWorkbenchPreferences } from "../lib/workbench-preferences";
import { fetchWithTimeout } from "../protocol/api";
import type { LLMSettingsPayload } from "./settingsShared";
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(), fetchWithTimeout: vi.fn() }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<typeof import("../protocol/ws-outbox")>(), sendClientCommandAwaitResult: vi.fn(async () => ({ type: "command.result", level: "success" })) }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
const payload: LLMSettingsPayload = { provider: "custom", active_model: "local-model", provider_history: [], custom: {
  display_name: "Local speech gateway", base_url: "http://127.0.0.1:9123/v1", model: "local-model", available_models: ["local-model"], wire_api: "chat", has_api_key: false, auth_header: false,
}};
beforeEach(() => {
  useAppStore.setState({ conversationId: null, workingDirectory: "C:/voice", draft: "authored draft", workbenchPreferences: { ...defaultWorkbenchPreferences, dictationDictionary: ["retained phrase"] } });
  vi.mocked(fetchWithTimeout).mockImplementation(async (_url, options) => {
    const request = JSON.parse(String(options?.body)); return new Response(JSON.stringify({ provider: "custom", custom: request.custom }));
  });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it("uses the existing provider Save action for the transcription model and preserves unrelated preferences and drafts", async () => {
  const invalidated = vi.fn(); window.addEventListener("dictation:service-changed", invalidated);
  render(<ProviderTab selectedProvider="custom" settingsPayload={payload} settingsPayloadRef={{ current: payload }} onProviderChange={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "编辑 Local speech gateway" })); fireEvent.click(screen.getByText("高级设置"));
  fireEvent.change(screen.getByRole("textbox", { name: "转录模型" }), { target: { value: " asr-provider-model " } });
  expect(useAppStore.getState().workbenchPreferences.speechModel).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  await waitFor(() => expect(useAppStore.getState().workbenchPreferences).toMatchObject({ speechProvider: "custom", speechModel: "asr-provider-model", speechEnabled: true, dictationDictionary: ["retained phrase"] }));
  expect(useAppStore.getState().draft).toBe("authored draft");
  expect(invalidated).toHaveBeenCalledOnce(); window.removeEventListener("dictation:service-changed", invalidated);
});
it("does not publish a dictation service configuration before provider persistence succeeds", async () => {
  vi.mocked(fetchWithTimeout).mockResolvedValueOnce(new Response(JSON.stringify({ detail: "fixture save error" }), { status: 503 }));
  render(<ProviderTab selectedProvider="custom" settingsPayload={payload} settingsPayloadRef={{ current: payload }} onProviderChange={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "编辑 Local speech gateway" })); fireEvent.click(screen.getByText("高级设置"));
  fireEvent.change(screen.getByRole("textbox", { name: "转录模型" }), { target: { value: "preserve unsaved speech draft" } });
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  await waitFor(() => expect(fetchWithTimeout).toHaveBeenCalledOnce());
  await waitFor(() => expect((screen.getByRole("button", { name: "保存", exact: true }) as HTMLButtonElement).disabled).toBe(false));
  expect(useAppStore.getState().workbenchPreferences.speechEnabled).toBe(false);
  expect((screen.getByRole("textbox", { name: "转录模型" }) as HTMLInputElement).value).toBe("preserve unsaved speech draft");
});
