// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { useDesktopTerminalMirror } from "./useDesktopTerminalMirror";
import { sendClientCommand } from "../protocol/ws-outbox";
import { ptySnapshot } from "../desktop/runtime";

const native = vi.hoisted(() => ({
  onData: new Set<(event: { sessionId: string; conversationId: string; data: string; startCursor: number; endCursor: number }) => void>(),
  onExit: new Set<(event: { sessionId: string; conversationId: string; exitCode: number | null; exitSignal?: string; exitedAt?: number }) => void>(),
}));
vi.mock("../desktop/runtime", () => ({
  isDesktop: () => true,
  desktop: () => ({ pty: {
    onData: (callback: (event: never) => void) => { native.onData.add(callback); return () => native.onData.delete(callback); },
    onExit: (callback: (event: never) => void) => { native.onExit.add(callback); return () => native.onExit.delete(callback); },
  } }),
  ptyList: vi.fn(async () => [{ sessionId: "term-native", conversationId: "owner" }]),
  ptySnapshot: vi.fn(),
}));
vi.mock("../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(), sendClientCommand: vi.fn(() => true),
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

function RendererOwner() { useDesktopTerminalMirror(); return null; }
const snapshot = (output = "tick\r\n", end = 6) => ({
  sessionId: "term-native", conversationId: "owner", pid: 10, cwd: "C:/audit", shell: "pwsh",
  output, outputStartCursor: end - output.length, outputEndCursor: end, isAlive: true,
});
const emitOutput = () => { for (const callback of native.onData) callback({ sessionId: "term-native", conversationId: "owner", data: "tick\r\n", startCursor: 6, endCursor: 12 }); };

beforeEach(() => {
  vi.clearAllMocks();
  native.onData.clear(); native.onExit.clear();
  useAppStore.setState({ isConnected: true, conversationId: "owner", conversations: [], dockCollapsed: true,
    terminalSessions: [{ id: "term-native", conversationId: "owner", cwd: "C:/audit", shell: "pwsh", status: "running", terminalMode: "pty" }],
  });
  vi.mocked(ptySnapshot).mockResolvedValue(snapshot());
});
afterEach(cleanup);

it("mirrors output and exit while the terminal panel is closed", async () => {
  render(<RendererOwner />);
  await waitFor(() => expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.mirror.created", output: "tick\r\n", output_end_cursor: 6 }), { silent: true }));
  act(emitOutput);
  act(() => { for (const callback of native.onExit) callback({ sessionId: "term-native", conversationId: "owner", exitCode: 0 }); });
  expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.mirror.output", start_cursor: 6, end_cursor: 12 }), { silent: true });
  expect(sendClientCommand).toHaveBeenLastCalledWith(expect.objectContaining({ type: "terminal.mirror.exit", exit_code: 0 }), { silent: true });
  expect(useAppStore.getState().terminalSessions[0].status).toBe("exited");
});

it("keeps signal exit metadata in a closed-dock mirror and renderer state", async () => {
  render(<RendererOwner />);
  await waitFor(() => expect(sendClientCommand).toHaveBeenCalledTimes(1));
  act(() => { for (const callback of native.onExit) callback({ sessionId: "term-native", conversationId: "owner", exitCode: null, exitSignal: "SIGTERM", exitedAt: 2900 }); });
  expect(sendClientCommand).toHaveBeenLastCalledWith(expect.objectContaining({ type: "terminal.mirror.exit", exit_code: null, exit_signal: "SIGTERM", exited_at: 2900 }), { silent: true });
  expect(useAppStore.getState().terminalSessions[0]).toMatchObject({ status: "exited", exitCode: null, exitSignal: "SIGTERM", exitedAt: 2900 });
});

it("registers a fresh authoritative snapshot after reconnect without an endpoint change", async () => {
  render(<RendererOwner />);
  await waitFor(() => expect(sendClientCommand).toHaveBeenCalledTimes(1));
  act(() => useAppStore.setState({ isConnected: false }));
  vi.mocked(sendClientCommand).mockClear();
  vi.mocked(ptySnapshot).mockResolvedValue(snapshot("tick\r\ntick\r\n", 12));
  act(() => useAppStore.setState({ isConnected: true }));
  await waitFor(() => expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.mirror.created", output: "tick\r\ntick\r\n", output_end_cursor: 12 }), { silent: true }));
});

it("forwards chunks received during one snapshot wait after registration", async () => {
  let finish!: (value: Awaited<ReturnType<typeof ptySnapshot>>) => void;
  vi.mocked(ptySnapshot).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  render(<RendererOwner />);
  act(emitOutput);
  expect(sendClientCommand).not.toHaveBeenCalled();
  await act(async () => finish(snapshot()));
  expect(vi.mocked(sendClientCommand).mock.calls.map(([command]) => command.type)).toEqual(["terminal.mirror.created", "terminal.mirror.output"]);
});

it("does not publish a pending capture from a disconnected renderer generation", async () => {
  let finish!: (value: Awaited<ReturnType<typeof ptySnapshot>>) => void;
  vi.mocked(ptySnapshot).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  render(<RendererOwner />);
  act(emitOutput);
  act(() => useAppStore.setState({ isConnected: false }));
  await act(async () => finish(snapshot()));
  expect(sendClientCommand).not.toHaveBeenCalled();
  expect(native.onData.size).toBe(0);
});
