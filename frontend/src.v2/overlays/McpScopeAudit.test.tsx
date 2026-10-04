// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConnectorsTab } from "./ConnectorsTab";
import { useAppStore } from "../stores";
import { handlePeripheralEvent } from "../chat/peripheralEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { handleSessionEvent } from "../chat/sessionEvents";
import { createStreamBuffer } from "../lib/stream-buffer";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";

vi.mock("../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(),
  sendClientCommandAwaitResult: vi.fn(), sendClientCommand: vi.fn(() => true),
}));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
beforeEach(() => {
  vi.clearAllMocks();
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/workspace-A", mcpServers: [{ name: "shared", status: "connected", phase: "connected", capabilities: { resources: true } }] });
});
afterEach(cleanup);

it("ignores old workspace status, lifecycle and progress after a switch", () => {
  useAppStore.setState({ conversationId: "B", workingDirectory: "C:/workspace-B" });
  handlePeripheralEvent({ type: "mcp_status", conversation_id: "A", workspace_root: "C:/workspace-A", servers: [{ name: "old-A", status: "connected" }] });
  handleRuntimeEvent({ type: "mcp.lifecycle", conversation_id: "A", workspace_root: "C:/workspace-A", server_name: "shared", phase: "failed" });
  handleRuntimeEvent({ type: "mcp.progress", conversation_id: "A", workspace_root: "C:/workspace-A", server_name: "shared", operation: "connect", status: "failed" });
  expect(useAppStore.getState().mcpServers).toEqual([{ name: "shared", status: "connected", phase: "connected", capabilities: { resources: true } }]);
  handlePeripheralEvent({ type: "mcp_status", conversation_id: "B", workspace_root: "C:/workspace-B", servers: [{ name: "current-B", status: "connected" }] });
  expect(useAppStore.getState().mcpServers[0].name).toBe("current-B");
});

it("also rejects the old workspace when the conversation identity is unchanged", () => {
  useAppStore.setState({ workingDirectory: "C:/workspace-new" });
  handlePeripheralEvent({ type: "mcp_status", conversation_id: "A", workspace_root: "C:/workspace-A", servers: [{ name: "old-A", status: "connected" }] });
  expect(useAppStore.getState().mcpServers[0].name).toBe("shared");
});

it("clears the previous MCP catalog when one conversation changes workspace", () => {
  useAppStore.getState().setWorkingDirectory("c:\\workspace-A\\");
  expect(useAppStore.getState().mcpServers[0].name).toBe("shared");
  useAppStore.getState().setWorkingDirectory("C:/workspace-empty");
  expect(useAppStore.getState().mcpServers).toEqual([]);
  handlePeripheralEvent({ type: "mcp_status", conversation_id: "A", workspace_root: "C:/workspace-A", servers: [{ name: "late-A", status: "connected" }] });
  expect(useAppStore.getState().mcpServers).toEqual([]);
  handlePeripheralEvent({ type: "mcp_status", conversation_id: "A", workspace_root: "C:/workspace-empty", servers: [] });
  expect(useAppStore.getState().mcpServers).toEqual([]);
});

it("requests the committed owner's catalog when startup precedes the switch event", () => {
  handlePeripheralEvent({ type: "mcp_status", conversation_id: "B", workspace_root: "C:/workspace-B", servers: [{ name: "current-B", status: "connected" }] });
  expect(useAppStore.getState().mcpServers[0].name).toBe("shared");
  handleSessionEvent({ type: "conversation.switched", conversation_id: "B", conversation: {
    id: "B", title: "B", workspace_root: "C:/workspace-B", transcript: [],
  } }, { textStreamBuffer: createStreamBuffer(() => {}), thinkingStreamBuffer: createStreamBuffer(() => {}) });
  expect(sendClientCommand).toHaveBeenCalledWith({
    type: "mcp.list", conversation_id: "B", workspace_root: "C:/workspace-B",
  }, { silent: true });
  handlePeripheralEvent({ type: "mcp_status", conversation_id: "B", workspace_root: "C:/workspace-B", servers: [{ name: "current-B", status: "connected" }] });
  expect(useAppStore.getState().mcpServers[0].name).toBe("current-B");
});

it("does not reuse or publish same-name inventory from a previous workspace", async () => {
  const completions: Array<(value: Awaited<ReturnType<typeof sendClientCommandAwaitResult>>) => void> = [];
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(() => new Promise((resolve) => completions.push(resolve)));
  render(<ConnectorsTab />);
  fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 shared" }));
  await waitFor(() => expect(completions).toHaveLength(1));
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/workspace-B" }));
  fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 shared" }));
  await waitFor(() => expect(completions).toHaveLength(2));
  const result = (name: string) => ({ type: "command.result" as const, command: "mcp.inventory.list", level: "success", message: "", data: { inventory: { server_name: "shared", resources: [{ name, uri: `audit://${name}` }], resource_templates: [], prompts: [] } } });
  await act(async () => completions[0](result("A-resource")));
  expect(screen.queryByText("A-resource")).toBeNull();
  await act(async () => completions[1](result("B-resource")));
  expect(screen.getByText("B-resource")).toBeTruthy();
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls[0][0]).toMatchObject({ conversation_id: "A", workspace_root: "C:/workspace-A" });
  expect(vi.mocked(sendClientCommandAwaitResult).mock.calls[1][0]).toMatchObject({ conversation_id: "B", workspace_root: "C:/workspace-B" });
});
