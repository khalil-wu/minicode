/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { TerminalPanel } from "./TerminalPanel";

const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  });
  return {
    native: true,
    input: null as ((data: string) => void) | null,
    output: null as ((event: { sessionId: string; conversationId: string; data: string }) => void) | null,
    list: vi.fn(), snapshot: vi.fn(), spawn: vi.fn(), restart: vi.fn(), clearPty: vi.fn(),
    write: vi.fn(), resize: vi.fn(), kill: vi.fn(), ackExit: vi.fn(),
    send: vi.fn(), awaitResult: vi.fn(), clearScreen: vi.fn(), renderOutput: vi.fn(),
    resetScreen: vi.fn(), terminalOptions: {} as { convertEol?: boolean; fontFamily?: string },
  };
});

vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ subscribe: () => () => {} }) }));
vi.mock("../desktop/runtime", async (importOriginal) => ({
  ...await importOriginal<typeof import("../desktop/runtime")>(),
  isDesktop: () => mocks.native,
  desktop: () => ({ pty: {
    onData: (callback: typeof mocks.output) => { mocks.output = callback; return () => {}; },
    onExit: () => () => {},
  } }),
  ptyList: mocks.list, ptySnapshot: mocks.snapshot, ptySpawn: mocks.spawn,
  ptyRestart: mocks.restart, ptyClear: mocks.clearPty, ptyWrite: mocks.write,
  ptyResize: mocks.resize, ptyKill: mocks.kill, ptyAckExit: mocks.ackExit,
}));
vi.mock("../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.send,
  sendClientCommandAwaitResult: mocks.awaitResult,
}));
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 80;
  rows = 24;
  options = mocks.terminalOptions;
  constructor(options: { convertEol?: boolean }) { Object.assign(this.options, options); }
  clear = mocks.clearScreen;
  reset = mocks.resetScreen;
  write = mocks.renderOutput;
  writeln = mocks.renderOutput;
  onData(callback: (data: string) => void) { mocks.input = callback; }
  loadAddon() {}
  open() {}
  dispose() {}
  focus() {}
  attachCustomKeyEventHandler() {}
} }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));

const pending = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

const pty = (owner = "conv-a", id = "term-a", isAlive = true) => ({
  sessionId: id, conversationId: owner, cwd: `C:/${owner}`, shell: "pwsh", isAlive,
  terminalMode: "pty" as const, output: `${owner} output`, outputStartCursor: 0, outputEndCursor: 20,
});

const switchOwner = async (owner: string) => {
  await act(async () => useAppStore.setState({
    conversationId: owner, workingDirectory: `C:/${owner}`,
    terminalSessions: [], terminalSnapshots: {}, activeTerminalSessionId: null,
  }));
};

describe("terminal lifecycle", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mocks.native = true;
    mocks.input = null;
    mocks.output = null;
    mocks.terminalOptions = {};
    mocks.list.mockResolvedValue([]);
    mocks.snapshot.mockImplementation(async (id: string, owner: string) => pty(owner, id));
    mocks.spawn.mockImplementation(async (_cwd: string, owner: string) => pty(owner, `${owner}-new`));
    mocks.write.mockResolvedValue(true);
    mocks.resize.mockResolvedValue(true);
    mocks.send.mockReturnValue(true);
    mocks.awaitResult.mockImplementation(async ({ type }: { type: string }) => ({
      command: type, level: type === "terminal.create" ? "error" : "success",
      message: type === "terminal.create" ? "Interactive shell unavailable" : "OK", data: {},
    }));
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
    useAppStore.setState({
      conversationId: "conv-a", workingDirectory: "C:/conv-a", terminalSessions: [],
      activeTerminalSessionId: null, terminalSnapshots: {}, conversationWorkbenchStates: {},
      previewServers: [], resolvedTheme: "dark",
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    document.documentElement.style.removeProperty("--font-mono");
  });

  it("uses the bundled CJK face after the monospace fonts in the shared code token", async () => {
    const tokensCss = readFileSync("src.v2/styles/tokens.css", "utf8");
    const stack = tokensCss.match(/--font-mono:\s*([^;]+);/)?.[1];
    expect(stack).toContain('"Noto Sans SC"');
    expect(stack!.indexOf('"Noto Sans SC"')).toBeGreaterThan(stack!.indexOf('"JetBrains Mono"'));
    document.documentElement.style.setProperty("--font-mono", stack!);

    render(<TerminalPanel />);

    await waitFor(() => expect(mocks.terminalOptions.fontFamily).toBe(stack));
  });

  it.each([true, false])("does not start a terminal or promise a command runner without a workspace (desktop=%s)", async (native) => {
    mocks.native = native;
    useAppStore.setState({ workingDirectory: null });
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.input).toBeTypeOf("function"));

    fireEvent.click(screen.getByRole("button", { name: "新建终端" }));
    await screen.findByText("请先打开工作区，再启动终端或运行命令。");
    act(() => mocks.input!("echo unexpected\r"));

    expect(mocks.spawn).not.toHaveBeenCalled();
    expect(mocks.awaitResult).not.toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.create" }), expect.anything());
    expect(mocks.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.exec" }));
    expect(mocks.renderOutput.mock.calls.some(([output]) => String(output).includes("已就绪"))).toBe(false);
    expect(mocks.renderOutput).not.toHaveBeenCalledWith("$ ");
  });

  it("starts after a workspace is mounted into the same conversation", async () => {
    useAppStore.setState({ workingDirectory: null });
    render(<TerminalPanel />);
    await screen.findByText("请先打开工作区，再启动终端或运行命令。");

    await act(async () => useAppStore.setState({ workingDirectory: "C:/mounted" }));

    await waitFor(() => expect(mocks.spawn).toHaveBeenCalledWith("C:/mounted", "conv-a"));
  });

  it("rejects command-runner input immediately when its workspace is cleared and discards the old draft", async () => {
    mocks.native = false;
    render(<TerminalPanel />);
    await screen.findByText("Interactive shell unavailable");
    act(() => mocks.input!("echo old-draft"));

    act(() => {
      useAppStore.setState({ workingDirectory: null });
      mocks.input!("\r");
    });
    expect(mocks.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.exec" }));
    await act(async () => useAppStore.setState({ workingDirectory: "C:/mounted" }));
    await screen.findByText("Interactive shell unavailable");
    act(() => mocks.input!("echo current\r"));

    expect(mocks.send).toHaveBeenLastCalledWith({
      type: "terminal.exec", command: "echo current", cwd: "C:/mounted",
      conversation_id: "conv-a", workspace_root: "C:/mounted",
    });
  });

  it("does not enable a late command runner after the workspace was cleared", async () => {
    const oldSpawn = pending<ReturnType<typeof pty> | null>();
    mocks.spawn.mockReturnValueOnce(oldSpawn.promise);
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.spawn).toHaveBeenCalled());
    await act(async () => useAppStore.setState({ workingDirectory: null }));

    await act(async () => oldSpawn.resolve(null));
    act(() => mocks.input!("echo unexpected\r"));

    expect(screen.queryByText(/命令运行器已就绪/)).toBeNull();
    expect(mocks.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.exec" }));
  });

  it("resets the screen and selects newline handling for the active transport", async () => {
    mocks.native = false;
    const pipe = {
      id: "term-pipe", conversationId: "conv-a", cwd: "C:/conv-a", shell: "bash",
      terminalMode: "pipe" as const,
    };
    const native = { ...pipe, id: "term-pty", shell: "pwsh", terminalMode: "pty" as const };
    const pipeOutput = "first\nsecond\n";
    const nativeOutput = "native\r\noutput\r\n";
    const rendered: { output: string; convertEol?: boolean }[] = [];
    mocks.renderOutput.mockImplementation((output: string) => {
      rendered.push({ output, convertEol: mocks.terminalOptions.convertEol });
    });
    useAppStore.setState({
      terminalSessions: [pipe, native],
      activeTerminalSessionId: pipe.id,
      terminalSnapshots: {
        [pipe.id]: { ...pipe, output: pipeOutput, capturedAt: 1 },
        [native.id]: { ...native, output: nativeOutput, capturedAt: 1 },
      },
    });

    render(<TerminalPanel />);
    await waitFor(() => expect(rendered).toContainEqual({ output: pipeOutput, convertEol: true }));
    const resetCount = mocks.resetScreen.mock.calls.length;
    await act(async () => useAppStore.getState().setActiveTerminalSession(native.id));

    await waitFor(() => expect(rendered).toContainEqual({ output: nativeOutput, convertEol: false }));
    expect(mocks.resetScreen.mock.calls.length).toBeGreaterThan(resetCount);
    expect(mocks.clearScreen).not.toHaveBeenCalled();
  });

  it("updates newline handling when a cached session receives its transport metadata", async () => {
    mocks.native = false;
    const session = { id: "term-a", conversationId: "conv-a", cwd: "C:/conv-a", shell: "bash" };
    useAppStore.setState({ terminalSessions: [session], activeTerminalSessionId: session.id });
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.resetScreen).toHaveBeenCalled());
    expect(mocks.terminalOptions.convertEol).toBe(false);

    await act(async () => useAppStore.getState().upsertTerminalSession({ ...session, terminalMode: "pipe" }));

    await waitFor(() => expect(mocks.terminalOptions.convertEol).toBe(true));
  });

  it("keeps cached terminals after inventory failure and allows a manual retry", async () => {
    useAppStore.setState({
      terminalSessions: [{ id: "term-a", conversationId: "conv-a", cwd: "C:/conv-a", shell: "pwsh", status: "running" }],
      activeTerminalSessionId: "term-a",
    });
    mocks.list.mockRejectedValueOnce(new Error("Inventory unavailable")).mockResolvedValueOnce([pty()]);
    render(<TerminalPanel />);
    await screen.findByText(/Inventory unavailable/);

    expect(useAppStore.getState().activeTerminalSessionId).toBe("term-a");
    expect(mocks.spawn).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "刷新终端列表" }));
    await waitFor(() => expect(mocks.snapshot).toHaveBeenCalled());
    expect(screen.queryByText(/Inventory unavailable/)).toBeNull();
  });

  it("does not auto-create a terminal when the initial inventory request failed", async () => {
    mocks.list.mockRejectedValue(new Error("Inventory unavailable"));
    render(<TerminalPanel />);
    await screen.findByText(/Inventory unavailable/);
    await waitFor(() => expect(mocks.input).toBeTypeOf("function"));

    expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it("ignores inventory errors from a conversation that is no longer selected", async () => {
    const oldList = pending<ReturnType<typeof pty>[]>();
    mocks.list.mockReturnValueOnce(oldList.promise).mockResolvedValueOnce([pty("conv-b", "term-b")]);
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.list).toHaveBeenCalledWith("conv-a"));
    await switchOwner("conv-b");
    await waitFor(() => expect(useAppStore.getState().activeTerminalSessionId).toBe("term-b"));

    await act(async () => oldList.reject(new Error("Old inventory failure")));

    expect(useAppStore.getState().activeTerminalSessionId).toBe("term-b");
    expect(screen.queryByText(/Old inventory failure/)).toBeNull();
    expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it("shows startup errors without sending typed input to a different runner", async () => {
    mocks.spawn.mockRejectedValue(new Error("Shell startup denied"));
    render(<TerminalPanel />);
    await screen.findByText(/Shell startup denied/);

    act(() => mocks.input!("echo unexpected\r"));

    expect(mocks.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.exec" }));
    expect(mocks.write).not.toHaveBeenCalled();
  });

  it("starts the selected conversation's terminal after an earlier startup finishes", async () => {
    const oldSpawn = pending<ReturnType<typeof pty>>();
    mocks.spawn.mockReturnValueOnce(oldSpawn.promise);
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.spawn).toHaveBeenCalledWith("C:/conv-a", "conv-a"));
    await switchOwner("conv-b");
    await waitFor(() => expect(mocks.list).toHaveBeenCalledWith("conv-b"));

    await act(async () => oldSpawn.resolve(pty("conv-a", "term-a")));

    await waitFor(() => expect(mocks.spawn).toHaveBeenCalledWith("C:/conv-b", "conv-b"));
    expect(useAppStore.getState().activeTerminalSessionId).toBe("conv-b-new");
  });

  it("keeps the selected terminal when an old startup fails", async () => {
    const oldSpawn = pending<ReturnType<typeof pty>>();
    mocks.spawn.mockReturnValueOnce(oldSpawn.promise);
    mocks.list.mockResolvedValueOnce([]).mockResolvedValueOnce([pty("conv-b", "term-b")]);
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.spawn).toHaveBeenCalled());
    await switchOwner("conv-b");
    await waitFor(() => expect(useAppStore.getState().activeTerminalSessionId).toBe("term-b"));

    await act(async () => oldSpawn.reject(new Error("Old startup failure")));

    expect(useAppStore.getState().activeTerminalSessionId).toBe("term-b");
    expect(screen.queryByText(/Old startup failure/)).toBeNull();
  });

  it("uses the current owner and directory for command-runner input and clears old drafts", async () => {
    mocks.native = false;
    render(<TerminalPanel />);
    await screen.findByText("Interactive shell unavailable");
    act(() => mocks.input!("echo old-draft"));
    await switchOwner("conv-b");
    await waitFor(() => expect(mocks.awaitResult).toHaveBeenCalledWith(
      expect.objectContaining({ type: "terminal.create", conversation_id: "conv-b" }), "terminal.create",
    ));
    await screen.findByText("Interactive shell unavailable");

    act(() => mocks.input!("echo current\r"));

    expect(mocks.send).toHaveBeenLastCalledWith({
      type: "terminal.exec", command: "echo current", cwd: "C:/conv-b",
      conversation_id: "conv-b", workspace_root: "C:/conv-b",
    });
  });

  it("does not run commands from an exited terminal in the command runner", async () => {
    mocks.list.mockResolvedValue([pty("conv-a", "term-a", false)]);
    mocks.snapshot.mockResolvedValue(pty("conv-a", "term-a", false));
    render(<TerminalPanel />);
    await screen.findByRole("button", { name: "重新启动终端" });
    await waitFor(() => expect(mocks.input).toBeTypeOf("function"));

    act(() => mocks.input!("echo unexpected\r"));

    expect(screen.getByText("终端会话已停止，请重新启动或新建终端。")).toBeTruthy();
    expect(mocks.send).not.toHaveBeenCalledWith(expect.objectContaining({ type: "terminal.exec" }));
    expect(mocks.write).not.toHaveBeenCalled();
  });

  it.each(["清空终端", "重新启动终端"])("reports failures from %s in the panel", async (label) => {
    mocks.list.mockResolvedValue([pty("conv-a", "term-a", false)]);
    mocks.snapshot.mockResolvedValue(pty("conv-a", "term-a", false));
    mocks.clearPty.mockRejectedValue(new Error("Operation denied"));
    mocks.restart.mockRejectedValue(new Error("Operation denied"));
    render(<TerminalPanel />);
    await screen.findByRole("button", { name: "重新启动终端" });

    fireEvent.click(screen.getByRole("button", { name: label }));

    expect(await screen.findByText(/Operation denied/)).toBeTruthy();
    expect(useAppStore.getState().activeTerminalSessionId).toBe("term-a");
  });

  it("does not publish a background conversation's server URL into the selected conversation", async () => {
    mocks.list.mockResolvedValue([pty("conv-b", "term-b")]);
    await switchOwner("conv-b");
    render(<TerminalPanel />);
    await screen.findByRole("tab", { name: "pwsh" });

    act(() => mocks.output!({ sessionId: "term-a", conversationId: "conv-a", data: "http://localhost:3123" }));

    expect(useAppStore.getState().previewServers).toEqual([]);
    expect(screen.queryByText("localhost:3123")).toBeNull();
  });
});
