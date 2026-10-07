/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { McpToolsEditor } from "./McpToolsEditor";
import { useAppStore } from "../stores";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { McpServerStatus } from "../stores/types";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/ws-outbox", async (original) => ({ ...await original<typeof import("../protocol/ws-outbox")>(), sendClientCommand: vi.fn(),
  sendClientCommandAwaitResult: vi.fn(async () => ({ type: "command.result", level: "success", command: "mcp.update", message: "", data: {} })) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("synchronizes the allow-all draft before a later selected-tools save", async () => {
  useAppStore.setState({ workingDirectory: "/mcp-tools-owner" });
  render(<McpToolsEditor server={{ name: "mcp", editable: true, enabledTools: [] } as McpServerStatus} tools={[{ name: "alpha" }, { name: "beta" }]} />);
  fireEvent.click(screen.getByRole("button", { name: "允许全部及新增工具" }));
  await waitFor(() => expect(screen.getAllByRole("checkbox").every((input) => (input as HTMLInputElement).checked)).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "保存所选工具" }));
  await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ enabled_tools: ["alpha", "beta"] }), "mcp.update"));
});

it("preserves the selection when allow-all is rejected", async () => {
  vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({ type: "command.result", level: "error", command: "mcp.update", message: "Rejected", data: {} });
  render(<McpToolsEditor server={{ name: "mcp", editable: true, enabledTools: ["alpha"] } as McpServerStatus} tools={[{ name: "alpha" }, { name: "beta" }]} />);
  fireEvent.click(screen.getByRole("button", { name: "允许全部及新增工具" }));
  await screen.findByText("Rejected");
  expect((screen.getAllByRole("checkbox")[0] as HTMLInputElement).checked).toBe(true);
  expect((screen.getAllByRole("checkbox")[1] as HTMLInputElement).checked).toBe(false);
});
