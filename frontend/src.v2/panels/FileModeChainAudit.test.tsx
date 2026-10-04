// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { DiffPanel } from "./DiffPanel";
import { TerminalPanel, mergeTerminalOutputByCursor } from "./TerminalPanel";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import { handlePeripheralEvent } from "../chat/peripheralEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import type { ServerEvent } from "../protocol/events";
import { sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";

const runtime = vi.hoisted(() => ({
  receive: null as ((event: unknown) => void) | null,
  terminal: null as { text: string } | null,
}));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ subscribe: (handler: (event: unknown) => void) => {
  runtime.receive = handler;
  return () => { runtime.receive = null; };
} }) }));
vi.mock("../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(),
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, desktop: () => null }));
vi.mock("../components/MonacoDiffView", () => ({ MonacoDiffView: () => null }));
vi.mock("../lib/monaco-colorize", () => ({
  useColorizedLines: () => [], extractFilePathFromDiff: () => "", guessLanguageFromPath: () => "text",
}));
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  text = "";
  cols = 80;
  rows = 24;
  options = {};
  constructor() { runtime.terminal = this; }
  reset() { this.text = ""; }
  clear() { this.text = ""; }
  write(data: string) { this.text += data; }
  writeln(data: string) { this.text += data + "\r\n"; }
  open() {}
  loadAddon() {}
  onData() {}
  dispose() {}
  focus() {}
} }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));

const terminalSnapshot = (output: string, start: number, end: number): ServerEvent => ({
  type: "terminal.snapshot", session_id: "term-one", conversation_id: "owner",
  terminal_mode: "pipe", output, output_start_cursor: start, output_end_cursor: end,
});
const terminalChunk = (data: string, start: number, end: number, owner = "owner") => ({
  type: "terminal.output", session_id: "term-one", conversation_id: owner,
  data, start_cursor: start, end_cursor: end,
});

beforeEach(() => {
  vi.clearAllMocks();
  runtime.terminal = null;
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { callback(0); return 1; });
  vi.mocked(sendClientCommandAwaitResult).mockImplementation(async (_command, command) => ({
    type: "command.result", command, level: "success", message: "",
  }));
  useAppStore.setState({
    conversationId: "owner", workingDirectory: "C:/audit", messages: [], diffReview: null,
    gitChanges: { workingTree: [{ path: "file.txt", patch: "", additions: 1, deletions: 0 }], staged: [], untracked: [], loading: false },
    requestGitChanges: vi.fn(), terminalSnapshots: {}, activeTerminalSessionId: "term-one",
    terminalSessions: [{ id: "term-one", conversationId: "owner", shell: "sh", cwd: "C:/audit", status: "running", terminalMode: "pipe" }],
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("file mode projection chain", () => {
  it("surfaces Git action errors and unlocks the same button for a retry", async () => {
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({
      type: "command.result", command: "diff.git_stage_file", level: "error", message: "index.lock exists",
    });
    render(<DiffPanel />);
    const stage = screen.getByRole("button", { name: "暂存 file.txt" }) as HTMLButtonElement;
    fireEvent.click(stage);
    await waitFor(() => expect(pushToast).toHaveBeenCalledWith("index.lock exists", "error", 5000));
    expect(stage.disabled).toBe(false);
    fireEvent.click(stage);
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(2));
  });

  it("only settles the loading query whose command id failed", () => {
    useAppStore.setState((state) => ({ gitChanges: { ...state.gitChanges, loading: true, workingTreeRequestId: "current-query" } }));
    const error: ServerEvent = { type: "command.result", command: "diff.git_working_tree", level: "error", message: "not a repository", client_command_id: "old-query" };
    handleCommandResultEvent(error);
    expect(useAppStore.getState().gitChanges.loading).toBe(true);
    handleCommandResultEvent({ ...error, client_command_id: "current-query" });
    expect(useAppStore.getState().gitChanges.loading).toBe(false);
    expect(useAppStore.getState().gitChanges.error).toBe("not a repository");
    expect(pushToast).toHaveBeenCalledWith("not a repository", "error", 5000);
  });

  it("appends identical post-snapshot output while ignoring stale owner and repeated cursor", async () => {
    handlePeripheralEvent(terminalSnapshot("tick\r\n", 0, 6));
    render(<TerminalPanel />);
    await waitFor(() => expect(runtime.terminal?.text).toBe("tick\r\n"));
    act(() => runtime.receive?.(terminalChunk("tick\r\n", 6, 12)));
    expect(runtime.terminal?.text).toBe("tick\r\ntick\r\n");
    act(() => runtime.receive?.(terminalChunk("tick\r\n", 6, 12)));
    act(() => runtime.receive?.(terminalChunk("foreign", 12, 19, "different-owner")));
    expect(runtime.terminal?.text).toBe("tick\r\ntick\r\n");
  });

  it("merges a snapshot arriving after a matching live chunk by cursor", async () => {
    render(<TerminalPanel />);
    await waitFor(() => expect(runtime.receive).not.toBeNull());
    await waitFor(() => expect(runtime.terminal).not.toBeNull());
    act(() => runtime.receive?.(terminalChunk("tick\r\n", 6, 12)));
    act(() => handlePeripheralEvent(terminalSnapshot("tick\r\n", 0, 6)));
    await waitFor(() => expect(runtime.terminal?.text).toBe("tick\r\ntick\r\n"));
  });

  it("keeps post-clear output that arrived before the clear result", async () => {
    handlePeripheralEvent(terminalSnapshot("tick\r\n", 0, 6));
    let completeClear!: (value: Awaited<ReturnType<typeof sendClientCommandAwaitResult>>) => void;
    vi.mocked(sendClientCommandAwaitResult).mockImplementation(async (_command, command) => {
      if (command === "terminal.clear") return new Promise((resolve) => { completeClear = resolve; });
      return { type: "command.result", command, level: "success", message: "" };
    });
    render(<TerminalPanel />);
    await waitFor(() => expect(runtime.terminal?.text).toBe("tick\r\n"));
    fireEvent.click(screen.getByRole("button", { name: "清空终端" }));
    act(() => runtime.receive?.(terminalChunk("next", 6, 10)));
    await act(async () => completeClear({ type: "command.result", command: "terminal.clear", level: "success", message: "", data: { output_cursor: 6 } }));
    expect(runtime.terminal?.text).toBe("next");
  });

  it("uses UTF-16 offsets and rejects malformed cursor spans at the protocol boundary", () => {
    expect(normalizeInboundServerEvent(terminalChunk("😀", 0, 2))).not.toBeNull();
    expect(normalizeInboundServerEvent(terminalChunk("😀", 0, 1))).toBeNull();
    expect(mergeTerminalOutputByCursor("😀", 0, 2, "😀😀", 4)).toEqual({ output: "😀😀", endCursor: 4 });
    expect(mergeTerminalOutputByCursor("old", 0, 3, "", 3)).toEqual({ output: "", endCursor: 3 });
  });
});
