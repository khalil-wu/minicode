/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ send: vi.fn() }));
vi.mock("../protocol/ws-outbox", async original => ({ ...(await original<Record<string, unknown>>()), sendPromptResponseCommand: mocks.send }));
import { useAppStore } from "../stores";
import { handleControlEvent } from "./controlEvents";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import type { ControlPermissionsRequest, ControlRequestEvent } from "../protocol/common-types";

const request = (): ControlRequestEvent & { request: ControlPermissionsRequest } => ({ type: "control_request", request_id: "grant", conversation_id: "owner", turn_id: "turn", message_id: "answer",
  request: { subtype: "request_permissions", tool_name: "request_permissions", tool_use_id: "grant", input: {},
    permissions: { network: { enabled: true }, file_system: { write: ["C:/workspace/review.txt"] } }, reason: "Edit the requested file", cwd: "C:/workspace" } });
beforeEach(() => {
  mocks.send.mockReset().mockResolvedValue({ type: "command.result", level: "success", command: "control_response", message: "" });
  useAppStore.setState({ conversationId: "owner", pendingApproval: null, approvalQueue: [], pendingDiffReview: null, diffReviewQueue: [], pendingAskUser: null,
    askUserQueue: [], subagents: [], draft: "retained draft", attachments: [{ id: "kept", name: "draft.txt", kind: "file", path: "draft.txt" }] as any });
});
afterEach(cleanup);
it("accepts the actual wire, replays the profile and submits turn authority with its original owner", async () => {
  const wire = normalizeInboundServerEvent(request())!;
  expect(wire).not.toBeNull(); handleControlEvent(wire);
  render(<InlineAgentPrompt />);
  expect(screen.getByText("连接互联网")).toBeTruthy(); expect(screen.getByTitle("C:/workspace/review.txt").textContent).toBe("review.txt");
  expect(screen.queryByRole("button", { name: "review.txt" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ conversation_id: "owner", turn_id: "turn", message_id: "answer",
    response: { subtype: "success", response: { permissions: request().request.permissions, scope: "turn" } } })));
  expect(useAppStore.getState().pendingApproval).toBeNull();
  expect(useAppStore.getState().draft).toBe("retained draft"); expect(useAppStore.getState().attachments[0].name).toBe("draft.txt");
});
it("denies with an empty profile, retaining the pending approval after a semantic refusal", async () => {
  mocks.send.mockResolvedValueOnce({ type: "command.result", level: "error", command: "control_response", message: "Owner changed" });
  handleControlEvent(request()); render(<InlineAgentPrompt />);
  fireEvent.click(screen.getByRole("button", { name: "拒绝使用工具" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ response: { subtype: "success", response: { permissions: {}, scope: "turn" } } })));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Owner changed");
  expect(useAppStore.getState().pendingApproval?.permissionRequest?.permissions).toEqual(request().request.permissions);
});
it("projects stdin as the source command approval without a conversation-scope option", () => {
  const stdin: ControlRequestEvent = { type: "control_request", request_id: "stdin", conversation_id: "owner", request: {
    subtype: "write_stdin", tool_name: "monitor", tool_use_id: "stdin", input: { action: "write_stdin", command_id: "owned", chars: "literal" },
    stdin_action: { display_command: "write_stdin --session-id 7 'literal'", input: "literal", cwd: "C:/workspace" } } };
  handleControlEvent(normalizeInboundServerEvent(stdin)!); render(<InlineAgentPrompt />);
  expect(screen.getByText("允许 MiniCode 运行此命令？")).toBeTruthy();
  expect(screen.getByLabelText("即将运行的命令").textContent).toBe("write_stdin --session-id 7 'literal'");
  expect(screen.queryByRole("button", { name: /^选择允许范围/ })).toBeNull();
});
