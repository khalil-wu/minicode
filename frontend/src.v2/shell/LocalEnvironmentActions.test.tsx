/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { WorkspaceRequestOwner } from "../protocol/workspace-owner";
const { read, send, owner } = vi.hoisted(() => ({ read: vi.fn(), send: vi.fn(() => true), owner: { current: undefined as WorkspaceRequestOwner | undefined } }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/local-environments", () => ({ activeLocalEnvironment: read }));
vi.mock("../protocol/workspace-owner", async original => ({ ...await original<typeof import("../protocol/workspace-owner")>(), workspaceRequestOwner: () => owner.current }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<typeof import("../protocol/ws-outbox")>(), sendClientCommand: send }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
import { LocalEnvironmentActions } from "./LocalEnvironmentActions";
import { useAppStore } from "../stores";

const payload = (revision = "revision-1") => ({ config_path: "C:/project/.minicode/environments/environment.toml", revision, environment: {}, actions: [{ index: 2, name: "Run tests", command: "npm test", icon: "test" }] });
beforeEach(() => {
  vi.clearAllMocks();
  owner.current = { session_id: "session-A", conversation_id: "A" };
  read.mockResolvedValue(payload());
  send.mockReturnValue(true);
  useAppStore.setState({ appMode: "cowork", defaultTerminalLocation: "bottom", dockCollapsed: true, activeBottomTab: "git", rightPanelExpanded: false,
    isConnected: true, conversationId: "A", workingDirectory: "C:/project", pendingConversationSwitchId: null, runtimeSession: null, rightPanelOpen: false, rightStackTab: "tasks", draft: "retained draft" });
});
afterEach(cleanup);

it("renders no launcher or request without a real workspace owner", async () => {
  owner.current = undefined;
  render(<LocalEnvironmentActions />);
  await act(async () => {});
  expect(read).not.toHaveBeenCalled();
  expect(screen.queryByRole("button")).toBeNull();
});

it.each(["bottom", "right"] as const)("executes a configured action in the preferred %s terminal using its owner and authoritative identity", async defaultTerminalLocation => {
  useAppStore.setState({ defaultTerminalLocation });
  render(<LocalEnvironmentActions />);
  fireEvent.click(await screen.findByRole("button", { name: "Run tests" }));
  expect(send).toHaveBeenCalledWith({ type: "terminal.exec", command: "npm test", cwd: "C:/project", workspace_root: "C:/project", conversation_id: "A",
    local_environment_action: { config_path: payload().config_path, revision: "revision-1", index: 2 } });
  expect(useAppStore.getState()).toMatchObject({ conversationId: "A", draft: "retained draft", appMode: "cowork", defaultTerminalLocation,
    ...(defaultTerminalLocation === "bottom" ? { dockCollapsed: false, activeBottomTab: "terminal", rightPanelOpen: false }
      : { rightPanelOpen: true, rightStackTab: "terminal", dockCollapsed: true, activeBottomTab: "git" }) });
});

it("does not expose an old owner's actions during a switch or after its read settles", async () => {
  let settle!: (value: ReturnType<typeof payload>) => void;
  read.mockReturnValueOnce(new Promise(resolve => { settle = resolve; }));
  render(<LocalEnvironmentActions />);
  await waitFor(() => expect(read).toHaveBeenCalledOnce());
  act(() => useAppStore.setState({ pendingConversationSwitchId: "B" }));
  await act(async () => settle(payload()));
  expect(screen.queryByRole("button")).toBeNull();
  expect(send).not.toHaveBeenCalled();
});

it("keeps one environment read while runtime progress and capabilities change for the same owner", async () => {
  useAppStore.setState({ runtimeSession: { session_id: "session-A", active_conversation_id: "A" } });
  let settle!: (value: ReturnType<typeof payload>) => void;
  read.mockReturnValueOnce(new Promise(resolve => { settle = resolve; }));
  render(<LocalEnvironmentActions />);
  await waitFor(() => expect(read).toHaveBeenCalledOnce());
  const signal = read.mock.calls[0][1] as AbortSignal;
  for (const session of [
    { session_id: "session-A", active_conversation_id: "A", active_task_id: "run-A", active_stream_conversation_ids: ["A"] },
    { session_id: "session-A", active_conversation_id: "A", selected_model: "another-model", pending_approval_count: 1,
      capabilities: { version: 2, mcp_registry_version: 3, skills: [] } },
    { session_id: "session-A", active_conversation_id: "A", task_summary: { completed: 1 }, active_stream_conversation_ids: [] },
  ]) {
    act(() => useAppStore.setState({ runtimeSession: session }));
  }
  expect(read).toHaveBeenCalledOnce();
  expect(signal.aborted).toBe(false);
  await act(async () => settle(payload()));
  expect(screen.getByRole("button", { name: "Run tests" })).toBeTruthy();
});

it("reads a new runtime owner instead of retaining the preceding session's actions", async () => {
  useAppStore.setState({ runtimeSession: { session_id: "session-A", active_conversation_id: "A" } });
  render(<LocalEnvironmentActions />);
  await screen.findByRole("button", { name: "Run tests" });
  let settle!: (value: ReturnType<typeof payload>) => void;
  read.mockReturnValueOnce(new Promise(resolve => { settle = resolve; }));
  act(() => {
    owner.current = { session_id: "session-B", conversation_id: "A" };
    useAppStore.setState({ runtimeSession: { session_id: "session-B", active_conversation_id: "A" } });
  });
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole("button")).toBeNull();
  await act(async () => settle(payload("session-B-revision")));
  fireEvent.click(screen.getByRole("button", { name: "Run tests" }));
  expect(send).toHaveBeenLastCalledWith(expect.objectContaining({
    local_environment_action: expect.objectContaining({ revision: "session-B-revision" }),
  }));
});

it("refreshes environment changes made outside the window when focus returns", async () => {
  render(<LocalEnvironmentActions />);
  await screen.findByRole("button", { name: "Run tests" });
  read.mockResolvedValueOnce(payload("external-revision"));
  act(() => window.dispatchEvent(new Event("focus")));
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole("button", { name: "Run tests" }));
  expect(send).toHaveBeenLastCalledWith(expect.objectContaining({
    local_environment_action: expect.objectContaining({ revision: "external-revision" }),
  }));
});

it("refreshes the committed action revision after save and keeps a rejected send from opening the terminal", async () => {
  render(<LocalEnvironmentActions />);
  await screen.findByRole("button", { name: "Run tests" });
  read.mockResolvedValueOnce(payload("revision-2"));
  act(() => window.dispatchEvent(new Event("local-environments:changed")));
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  send.mockReturnValue(false);
  fireEvent.click(screen.getByRole("button", { name: "Run tests" }));
  expect(send).toHaveBeenLastCalledWith(expect.objectContaining({ local_environment_action: expect.objectContaining({ revision: "revision-2" }) }));
  expect(useAppStore.getState()).toMatchObject({ rightPanelOpen: false, dockCollapsed: true, activeBottomTab: "git" });
});
