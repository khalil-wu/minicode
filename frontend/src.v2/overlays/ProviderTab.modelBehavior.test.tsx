/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ProviderTab } from "./ProviderTab";
import { fetchWithTimeout } from "../protocol/api";
import type { LLMSettingsPayload } from "./settingsShared";

vi.mock("../protocol/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/api")>(),
  fetchWithTimeout: vi.fn(),
}));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("edits each model independently and submits native tools and prompt settings in the saved profile", async () => {
  const payload: LLMSettingsPayload = { provider: "openai", openai: {
    base_url: "https://fixture.invalid/v1", api_key: "fixture", model: "alpha", wire_api: "responses",
    available_models: ["alpha", "beta"], model_metadata: {
      alpha: { context_window: 128000, model_instructions: "Alpha guidance" },
      beta: { model_instructions: "Beta guidance" },
    },
  }};
  payload.provider_history = [{ ...payload.openai, provider: "openai", provider_id: "openai" }];
  const saved = vi.fn();
  vi.mocked(fetchWithTimeout).mockImplementation(async (_url, options) => {
    const request = JSON.parse(String(options?.body));
    saved(request);
    return new Response(JSON.stringify({ provider: "openai", openai: request.openai }), { status: 200 });
  });
  render(<ProviderTab selectedProvider="openai" settingsPayload={payload} settingsPayloadRef={{ current: payload }} onProviderChange={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: /^编辑 / }));
  fireEvent.click(screen.getByText("高级设置"));
  const instructions = screen.getByLabelText("模型附加指令") as HTMLTextAreaElement;
  expect(instructions.value).toBe("Alpha guidance");
  fireEvent.change(instructions, { target: { value: "Edited alpha guidance" } });
  fireEvent.click(screen.getByLabelText(/原生补丁输入/));
  fireEvent.click(screen.getByLabelText(/WebSocket 增量传输/));
  fireEvent.click(screen.getByRole("button", { name: "上下文压缩方式，当前：自动选择" }));
  fireEvent.click(screen.getByRole("option", { name: "提供商原生压缩" }));
  fireEvent.click(screen.getByRole("button", { name: "默认模型，当前：alpha" }));
  fireEvent.click(screen.getByRole("option", { name: "beta" }));
  expect(instructions.value).toBe("Beta guidance");
  expect(screen.getByRole("button", { name: "上下文压缩方式，当前：自动选择" })).toBeTruthy();
  expect((screen.getByLabelText(/原生补丁输入/) as HTMLInputElement).checked).toBe(false);
  expect((screen.getByLabelText(/WebSocket 增量传输/) as HTMLInputElement).checked).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "默认模型，当前：beta" }));
  fireEvent.click(screen.getByRole("option", { name: "alpha" }));
  expect(instructions.value).toBe("Edited alpha guidance");
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(saved.mock.calls[0][0].openai.model_metadata).toEqual({
    alpha: { context_window: 128000, model_instructions: "Edited alpha guidance", supports_custom_tools: true, responses_websocket: true, native_compaction: true },
    beta: { model_instructions: "Beta guidance" },
  });
});
