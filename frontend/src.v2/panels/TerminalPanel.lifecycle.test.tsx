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
    output: null as ((event: { sessionId: string; conversationId: string; data: string; startCursor?: number; endCursor?: number }) => void) | null,
    list: vi.fn(), snapshot: vi.fn(), spawn: vi.fn(), restart: vi.fn(), clearPty: vi.fn(),
    write: vi.fn(), resize: vi.fn(), kill: vi.fn(), ackExit: vi.fn(),
    send: vi.fn(), awaitResult: vi.fn(), clearScreen: vi.fn(), renderOutput: vi.fn(),
    resetScreen: vi.fn(), terminalOptions: {} as { convertEol?: boolean; fontFamily?: string },
    viewport: { viewportY: 0, baseY: 0 },
    selection: undefined as { start: { x: number; y: number }; end: { x: number; y: number } } | undefined,
    selectText: vi.fn(), scrollToLine: vi.fn(), scrollToBottom: vi.fn(),
    instances: [] as Array<{ host?: HTMLElement; content: string; disposed: boolean; buffer: { active: { viewportY: number; baseY: number } }; selection?: { start: { x: number; y: number }; end: { x: number; y: number } } }>,
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
  content = "";
  disposed = false;
  host?: HTMLElement;
  screen?: HTMLPreElement;
  options: { convertEol?: boolean; fontFamily?: string };
  buffer = { active: { viewportY: 0, baseY: 0 } };
  selection?: { start: { x: number; y: number }; end: { x: number; y: number } };
  constructor(options: { convertEol?: boolean }) { this.options = { ...options }; mocks.terminalOptions = this.options; mocks.instances.push(this); }
  clear() { this.content = ""; this.screen!.textContent = ""; mocks.clearScreen(); }
  reset() { this.content = ""; this.screen!.textContent = ""; this.buffer.active = { viewportY: 0, baseY: 0 }; this.selection = undefined; mocks.resetScreen(); }
  write(data: string, onParsed?: () => void) {
    const following = this.buffer.active.viewportY >= this.buffer.active.baseY;
    this.content += data;
    this.screen!.textContent = this.content;
    this.buffer.active.baseY = Math.max(0, this.content.split("\n").length - this.rows);
    if (following) this.buffer.active.viewportY = this.buffer.active.baseY;
    mocks.terminalOptions = this.options;
    mocks.renderOutput(data);
    onParsed?.();
  }
  writeln(data: string) { this.write(`${data}\r\n`); }
  onData(callback: (data: string) => void) { mocks.input = callback; }
  loadAddon() {}
  open(host: HTMLElement) { this.host = host; this.screen = document.createElement("pre"); host.appendChild(this.screen); }
  dispose() { this.disposed = true; }
  focus() { mocks.terminalOptions = this.options; }
  attachCustomKeyEventHandler() {}
  getSelectionPosition() { return this.selection; }
  select = mocks.selectText;
  scrollToLine = mocks.scrollToLine;
  scrollToBottom = mocks.scrollToBottom;
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
    mocks.instances = [];
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
      previewServers: [], resolvedTheme: "dark", conversations: [],
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

  it("preserves previously read long output and each real session surface instead of rebuilding from the 80k cache", async () => {
    mocks.list.mockResolvedValue([pty("conv-a", "term-a"), pty("conv-a", "term-b")]);
    render(<TerminalPanel />);
    await screen.findByRole("tab", { name: "pwsh 1" });
    await waitFor(() => expect(mocks.instances.some((instance) => instance.host?.dataset.terminalSession === "term-a")).toBe(true));
    const first = mocks.instances.find((instance) => instance.host?.dataset.terminalSession === "term-a")!;
    const firstHost = first.host;
    const longOutput = Array.from({ length: 2000 }, (_, index) => `LOG_${String(index).padStart(4, "0")} ${"x".repeat(64)}\r\n`).join("");
    expect(longOutput.length).toBeGreaterThan(80_000);
    act(() => mocks.output!({ sessionId: "term-a", conversationId: "conv-a", data: longOutput, startCursor: 20, endCursor: 20 + longOutput.length }));
    first.buffer.active.viewportY = 80;
    first.selection = { start: { x: 0, y: 101 }, end: { x: 8, y: 101 } };
    const selectedLog = first.content.split("\n")[101];
    const resetCount = mocks.resetScreen.mock.calls.length;
    act(() => useAppStore.getState().setActiveTerminalSession("term-b"));
    const second = mocks.instances.find((instance) => instance.host?.dataset.terminalSession === "term-b")!;
    second.buffer.active.viewportY = 0;
    second.selection = { start: { x: 2, y: 0 }, end: { x: 5, y: 0 } };
    expect(firstHost!.hidden).toBe(true);
    act(() => mocks.output!({ sessionId: "term-a", conversationId: "conv-a", data: "BACKGROUND\r\n", startCursor: 20 + longOutput.length, endCursor: 20 + longOutput.length + 12 }));
    act(() => useAppStore.getState().setActiveTerminalSession("term-a"));
    expect(first.host).toBe(firstHost);
    expect(first.host!.hidden).toBe(false);
    expect(first.content).toContain("LOG_0000");
    expect(first.content).toContain("BACKGROUND");
    expect(first.content.split("\n")[101]).toBe(selectedLog);
    expect(first.buffer.active.viewportY).toBe(80);
    expect(first.selection).toEqual({ start: { x: 0, y: 101 }, end: { x: 8, y: 101 } });
    const endCursor = 20 + longOutput.length + 12;
    mocks.snapshot.mockImplementation(async (id: string, owner: string) => id === "term-a" ? { ...pty(owner, id), output: first.content.slice(-80_000), outputStartCursor: endCursor - 80_000, outputEndCursor: endCursor } : pty(owner, id));
    fireEvent.click(screen.getByRole("button", { name: "刷新终端列表" }));
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(2));
    expect(first.content).toContain("LOG_0000");
    expect(first.buffer.active.viewportY).toBe(80);
    expect(mocks.resetScreen).toHaveBeenCalledTimes(resetCount);
    act(() => useAppStore.getState().setActiveTerminalSession("term-b"));
    expect(second.host!.hidden).toBe(false);
    expect(second.selection).toEqual({ start: { x: 2, y: 0 }, end: { x: 5, y: 0 } });
  });

  it("releases a removed terminal surface and disposes all remaining instances at panel teardown", async () => {
    mocks.list.mockResolvedValue([pty("conv-a", "term-a"), pty("conv-a", "term-b")]);
    const { unmount } = render(<TerminalPanel />);
    await screen.findByRole("tab", { name: "pwsh 1" });
    await waitFor(() => expect(mocks.instances.some((instance) => instance.host?.dataset.terminalSession === "term-a")).toBe(true));
    const first = mocks.instances.find((instance) => instance.host?.dataset.terminalSession === "term-a")!;
    act(() => useAppStore.getState().setActiveTerminalSession("term-b"));
    act(() => useAppStore.getState().removeTerminalSession("term-a"));
    expect(first.disposed).toBe(true);
    expect(document.body.contains(first.host!)).toBe(false);
    unmount();
    expect(mocks.instances.every((instance) => instance.disposed)).toBe(true);
  });

  it("does not automatically start or focus a hidden terminal in a newly selected conversation", async () => {
    render(<TerminalPanel visible={false} />);
    await waitFor(() => expect(mocks.input).toBeTypeOf("function"));
    await switchOwner("conv-b");
    await waitFor(() => expect(mocks.list).toHaveBeenCalledWith("conv-b"));
    expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it.each([true, false])("does not start a terminal or promise a command runner without a workspace (desktop=%s)", async (native) => {
    mocks.native = native;
    useAppStore.setState({ workingDirectory: null });
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.input).toBeTypeOf("function"));

    fireEvent.click(screen.getByRole("button", { name: "新建终端" }));
    await screen.findByText("请先打开工作区，再启动终端或运行命令。");
    expect(screen.getAllByText("请先打开工作区，再启动终端或运行命令。")).toHaveLength(1);
    expect(screen.getByRole("status").textContent).toBe("请先打开工作区，再启动终端或运行命令。");
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

  it("retains separate source surfaces and selects newline handling for each active transport", async () => {
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
    expect(mocks.resetScreen.mock.calls.length).toBe(resetCount);
    expect(mocks.clearScreen).not.toHaveBeenCalled();
  });

  it("updates newline handling when a cached session receives its transport metadata", async () => {
    mocks.native = false;
    const session = { id: "term-a", conversationId: "conv-a", cwd: "C:/conv-a", shell: "bash" };
    useAppStore.setState({ terminalSessions: [session], activeTerminalSessionId: session.id });
    render(<TerminalPanel />);
    await waitFor(() => expect(mocks.instances.some((instance) => instance.host?.dataset.terminalSession === "term-a")).toBe(true));
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
