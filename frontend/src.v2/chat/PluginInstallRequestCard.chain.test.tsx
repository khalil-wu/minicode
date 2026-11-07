/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { handleControlEvent } from "./controlEvents";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";

const mocks = vi.hoisted(() => ({ response: vi.fn(), install: vi.fn() }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<Record<string, unknown>>(), sendPromptResponseCommand: mocks.response }));
vi.mock("../protocol/api", async original => ({ ...await original<Record<string, unknown>>(), fetchWithTimeout: mocks.install }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "window" }) }));

const event = () => ({ type: "control_request", request_id: "request_plugin_install_call", conversation_id: "owner", turn_id: "turn", message_id: "answer", replayed: true,
  workspace_root: "C:/project", request: { subtype: "elicitation", tool_use_id: "request_plugin_install_call", mode: "form",
    question: "Use the requested review plugin", prompt: "Use the requested review plugin", schema: { type: "object", properties: {} },
    _meta: { codex_approval_kind: "tool_suggestion", tool_type: "plugin", suggest_type: "install", tool_id: "review@market",
      tool_name: "Review Plugin", suggest_reason: "Use the requested review plugin", suggestion_id: "request_plugin_install_call", persist: "always" } } });
beforeEach(() => {
  mocks.response.mockReset().mockResolvedValue({ type: "command.result", command: "control_response", level: "success", message: "" });
  mocks.install.mockReset().mockResolvedValue({ ok: true, json: async () => ({ installed: true }) });
  useAppStore.setState({ conversationId: "owner", pendingConversationSwitchId: null, workingDirectory: "C:/project", draft: "Retained draft",
    attachments: [{ id: "attachment", name: "note.txt", type: "text/plain", size: 1, status: "ready" }],
    pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [], pendingAskUser: null, askUserQueue: [],
    promptDrafts: {}, subagents: [], isConnected: true });
  const normalized = normalizeInboundServerEvent(event());
  expect(normalized).not.toBeNull();
  handleControlEvent(normalized!);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("sends Accept only after actual installation and preserves the original draft and attachments", async () => {
  let finish!: (value: unknown) => void;
  mocks.install.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  render(<InlineAgentPrompt />);
  fireEvent.click(screen.getByRole("button", { name: /安装插件/ }));
  fireEvent.keyDown(document.body, { key: "Enter" });
  expect(mocks.install).toHaveBeenCalledOnce();
  expect(mocks.response).not.toHaveBeenCalled();
  expect(JSON.parse(mocks.install.mock.calls[0][1].body)).toEqual({ session_id: "window", request_id: "request_plugin_install_call",
    conversation_id: "owner", turn_id: "turn", message_id: "answer" });
  await act(async () => finish({ ok: true, json: async () => ({ installed: true }) }));
  await waitFor(() => expect(mocks.response).toHaveBeenCalledOnce());
  expect(mocks.response.mock.calls[0][0]).toMatchObject({ conversation_id: "owner", turn_id: "turn", message_id: "answer",
    response: { response: { action: "accept" } } });
  expect(useAppStore.getState()).toMatchObject({ draft: "Retained draft", attachments: [{ id: "attachment" }], pendingAskUser: null });
});

it("keeps installation failure retryable and never claims success", async () => {
  mocks.install.mockResolvedValueOnce({ ok: false, json: async () => ({ detail: "Plugin source changed" }) });
  render(<InlineAgentPrompt />);
  fireEvent.click(screen.getByRole("button", { name: /安装插件/ }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Plugin source changed");
  expect(mocks.response).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /安装插件/ }));
  await waitFor(() => expect(mocks.response).toHaveBeenCalledOnce());
});

it("declines persistently without installing and retains the request when the acknowledgement fails", async () => {
  mocks.response.mockResolvedValueOnce({ level: "error", message: "Original request expired" });
  render(<InlineAgentPrompt />);
  fireEvent.click(screen.getByRole("button", { name: "不再建议此插件" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Original request expired");
  expect(mocks.install).not.toHaveBeenCalled();
  expect(mocks.response.mock.calls[0][0].response.response).toEqual({ action: "decline", _meta: { persist: "always" } });
  expect(useAppStore.getState().pendingAskUser?.requestId).toBe("request_plugin_install_call");
});

it("keeps the installing request bound to its original conversation after navigation", async () => {
  let finish!: (value: unknown) => void;
  mocks.install.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  render(<InlineAgentPrompt />);
  fireEvent.click(screen.getByRole("button", { name: /安装插件/ }));
  act(() => useAppStore.setState({ conversationId: "other", draft: "Other draft" }));
  await act(async () => finish({ ok: true, json: async () => ({ installed: true }) }));
  await waitFor(() => expect(mocks.response).toHaveBeenCalledOnce());
  expect(mocks.response.mock.calls[0][0].conversation_id).toBe("owner");
  expect(useAppStore.getState().draft).toBe("Other draft");
});
