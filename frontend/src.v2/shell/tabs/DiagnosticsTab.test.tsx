/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { DiagnosticsTab } from "./DiagnosticsTab";

const { diagnose } = vi.hoisted(() => ({ diagnose: vi.fn() }));
vi.mock("../../protocol/api", () => ({ apiBase: () => "http://diagnosis.test", authHeaders: () => ({}), errorMessageFromResponseText: (text: string) => text, fetchWithTimeout: diagnose }));
vi.mock("../../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../../desktop/runtime", () => ({ isDesktop: () => false }));
beforeEach(() => { vi.clearAllMocks(); useAppStore.setState({ mcpServers: [], runtimeCapabilities: null, settingsOpen: false, currentModel: "model", terminalSessions: [] }); });
afterEach(cleanup);
const payload = (mcp: unknown[] = [], withSandbox = true) => ({ backend: { status: "ok" }, llm: { active_model: "model" }, mcp,
  capabilities: { tools: [], commands: [], skills: [], permission: withSandbox ? { sandbox_status: { probe_status: "ready", backend_available: true } } : undefined } });

it("reports MCP faults despite a successful backend response and links to its configuration", async () => {
  diagnose.mockResolvedValue({ ok: true, json: async () => payload([{ name: "broken", status: "error", error: "connection refused" }]) });
  render(<DiagnosticsTab />);
  expect(await screen.findByText("1 项需要处理")).toBeTruthy();
  expect(screen.getByText("MCP · broken")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "打开 MCP 设置" }));
  expect(useAppStore.getState().settingsTab).toBe("connectors");
  expect(useAppStore.getState().settingsOpen).toBe(true);
});

it("keeps missing detection data unknown and raw capability details collapsed", async () => {
  diagnose.mockResolvedValue({ ok: true, json: async () => payload([], false) });
  render(<DiagnosticsTab />);
  expect(await screen.findByText("状态未知")).toBeTruthy();
  expect(screen.getByText("展开运行能力和原始诊断").closest("details")?.open).toBe(false);
});

it("recomputes the summary when refreshing a fixed component", async () => {
  diagnose.mockResolvedValueOnce({ ok: true, json: async () => payload([{ name: "fixed", status: "error", error: "failed" }]) })
    .mockResolvedValue({ ok: true, json: async () => payload([{ name: "fixed", status: "connected" }]) });
  render(<DiagnosticsTab />);
  await screen.findByText("1 项需要处理");
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await waitFor(() => expect(screen.queryByText("1 项需要处理")).toBeNull());
  expect(await screen.findByText("当前已检测组件运行正常。")).toBeTruthy();
});

it("keeps the newest diagnosis when refresh responses arrive in reverse order", async () => {
  let finishOld!: (value: unknown) => void;
  diagnose.mockReturnValueOnce(new Promise((resolve) => { finishOld = resolve; }))
    .mockResolvedValue({ ok: true, json: async () => payload([{ name: "latest", status: "connected" }]) });
  render(<DiagnosticsTab />);
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  expect(await screen.findByText("当前已检测组件运行正常。")).toBeTruthy();
  await act(async () => finishOld({ ok: true, json: async () => payload([{ name: "stale", status: "error", error: "old" }]) }));
  expect(screen.queryByText("MCP · stale")).toBeNull();
  expect(screen.getByText("当前已检测组件运行正常。")).toBeTruthy();
});

it("does not apply another workspace's doctor projection to the current project", async () => {
  useAppStore.setState({ workingDirectory: "C:/current-project", workspaceGit: { branch: "current-branch", currentPath: "C:/current-project", modified: [], staged: [], untracked: [] } });
  diagnose.mockResolvedValue({ ok: true, json: async () => ({ ...payload([{ name: "foreign", status: "error", error: "old" }]),
    workspace: { root: "C:/foreign-project", writable: false }, git: { branch: "foreign-branch" } }) });
  render(<DiagnosticsTab />);
  await waitFor(() => expect(diagnose).toHaveBeenCalled());
  expect(await screen.findByText("current-project")).toBeTruthy();
  expect(screen.getByText("current-branch")).toBeTruthy();
  expect(screen.queryByText("foreign-project")).toBeNull();
  expect(screen.queryByText("MCP · foreign")).toBeNull();
  expect(screen.queryByText("工作区需要处理")).toBeNull();
});
