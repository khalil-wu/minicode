// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProviderTab } from "./ProviderTab";
import { SideChatPanel } from "../panels/SideChatPanel";
import { useAppStore } from "../stores";
import { fetchWithTimeout } from "../protocol/api";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { LLMSettingsPayload } from "./settingsShared";

vi.mock("../protocol/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/api")>(), fetchWithTimeout: vi.fn(),
}));
vi.mock("../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(),
  sendClientCommandAwaitResult: vi.fn(), sendClientCommand: vi.fn(() => true), sendConversationDeleteCommand: vi.fn(async () => true),
}));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../chat/cells/AssistantMarkdownCell", () => ({ AssistantMarkdownCell: () => null }));
vi.mock("../chat/tool-calls/ToolCallCard", () => ({ ToolCallCard: () => null }));

const profile = { provider: "openai", display_name: "Audit provider", base_url: "https://example.invalid/v1", model: "model-A", available_models: ["model-A"], wire_api: "responses" as const };
const payload = { provider: "openai", active_model: "model-A", openai: profile, provider_history: [profile] } as LLMSettingsPayload;
beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ conversationId: "conv-originA", workingDirectory: "C:/workspace-A", currentProvider: "openai", currentModel: "old-A", isConnected: true, sideChats: {}, messages: [], isStreaming: false });
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async (command, name) => ({
    type: "command.result", command: name, level: "success", message: "", data: { conversation_id: "conversation_id" in command ? command.conversation_id : "" },
  }));
});
afterEach(cleanup);

for (const switchOwner of [false, true]) it(`keeps provider save bound to its origin (switch=${switchOwner})`, async () => {
  let finish!: (response: Response) => void;
  vi.mocked(fetchWithTimeout).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  render(<ProviderTab selectedProvider="openai" settingsPayload={payload} settingsPayloadRef={{ current: payload }} onProviderChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "编辑 Audit provider" }));
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(fetchWithTimeout).toHaveBeenCalledTimes(1));
  if (switchOwner) act(() => useAppStore.setState({ conversationId: "conv-targetB", workingDirectory: "C:/workspace-B", currentProvider: "custom", currentModel: "pinned-B" }));
  await act(async () => finish({ ok: true, json: async () => payload } as Response));
  await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
    type: "llm.config.set", conversation_id: "conv-originA", workspace_root: "C:/workspace-A", provider: "openai", model: "model-A",
  }), "llm.config.set", { silent: true }));
  expect(useAppStore.getState().currentModel).toBe(switchOwner ? "pinned-B" : "model-A");
  expect(useAppStore.getState().currentProvider).toBe(switchOwner ? "custom" : "openai");
});

for (const committed of [false, true]) it(`shows side-chat timeout and retries the same id (committed=${committed})`, async () => {
  const created = new Set<string>();
  let attempts = 0;
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async (command, name) => {
    if (name !== "conversation.create" || command.type !== "conversation.create") throw new Error("unexpected command");
    const id = String(command.conversation_id);
    attempts += 1;
    if (attempts === 1) {
      if (committed) created.add(id);
      throw new Error("创建回执超时");
    }
    created.add(id);
    return { type: "command.result", command: name, level: "success", message: "", data: { conversation_id: id } };
  });
  render(<SideChatPanel />);
  await screen.findByRole("alert");
  expect(screen.getByRole("alert").textContent).toContain("创建回执超时");
  fireEvent.change(screen.getByRole("textbox", { name: "侧边对话消息" }), { target: { value: "continue" } });
  fireEvent.click(screen.getByRole("button", { name: "重试创建" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
  const ids = vi.mocked(sendClientCommandAwaitResult).mock.calls.map(([command]) => command.type === "conversation.create" ? command.conversation_id : "");
  expect(ids).toHaveLength(2);
  expect(ids[0]).toBe(ids[1]);
  expect(created.size).toBe(1);
});
