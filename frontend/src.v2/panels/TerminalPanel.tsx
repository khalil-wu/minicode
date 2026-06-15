import { useEffect, useRef, useState } from "react";
import { Copy, ExternalLink, Globe, Plus, RotateCcw, Square, Trash2 } from "lucide-react";
import "@xterm/xterm/css/xterm.css";
import { getWebSocket } from "../hooks/useWebSocket";
import { useAppStore } from "../stores";
import { desktop, isDesktop, ptyKill, ptyList, ptyResize, ptySpawn, ptyWrite } from "../desktop/runtime";
import type { TerminalSessionInfo } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";

type XtermLike = {
  cols: number;
  rows: number;
  clear: () => void;
  clearSelection?: () => void;
  dispose: () => void;
  focus?: () => void;
  getSelection?: () => string;
  hasSelection?: () => boolean;
  attachCustomKeyEventHandler?: (handler: (event: KeyboardEvent) => boolean) => void;
  loadAddon: (addon: unknown) => void;
  onData: (handler: (input: string) => void) => void;
  open: (element: HTMLElement) => void;
  write: (data: string) => void;
  writeln: (data: string) => void;
};

type FitAddonLike = {
  fit: () => void;
};

type XtermWithOptions = XtermLike & { options: { theme?: Record<string, string> } };

const DEV_SERVER_URL_RE = /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:[/?#][^\s'"<>]*)?/gi;
const AGENT_COMMAND_SESSION_ID = "agent-command-stream";

const normalizeDetectedUrl = (url: string): string =>
  url.replace(/^https?:\/\/0\.0\.0\.0/i, (prefix) => prefix.replace("0.0.0.0", "localhost"));

const portFromUrl = (url: string): number | null => {
  try {
    const parsed = new URL(url);
    const port = Number.parseInt(parsed.port, 10);
    return Number.isFinite(port) ? port : null;
  } catch {
    return null;
  }
};

const toTerminalNewlines = (value: string): string => value.replace(/\r?\n/g, "\r\n");

const shellName = (session: TerminalSessionInfo, index: number): string =>
  session.shell?.split(/[/\\]/).pop()?.replace(/\.(exe|cmd|ps1)$/i, "") || `shell ${index + 1}`;

const terminalTheme = (isLight: boolean): Record<string, string> => (
  isLight
    ? {
        background: "#fbfaf8",
        foreground: "#151515",
        cursor: "#0b78ff",
        selectionBackground: "#dfe9f8",
        black: "#2d2923",
        red: "#a8453f",
        green: "#4f7f45",
        yellow: "#9a742d",
        blue: "#5b6f8f",
        magenta: "#7b5f8f",
        cyan: "#4f7f78",
        white: "#8f8678",
        brightBlack: "#6f675d",
        brightRed: "#ba574f",
        brightGreen: "#5f9352",
        brightYellow: "#aa8538",
        brightBlue: "#6d7fa0",
        brightMagenta: "#8b6fa0",
        brightCyan: "#60928a",
        brightWhite: "#2d2923",
      }
    : {
        background: "#25211c",
        foreground: "#efe8dc",
        cursor: "#d7bd81",
        selectionBackground: "#51483d",
        black: "#1d1a16",
        red: "#dc7f72",
        green: "#8fbe76",
        yellow: "#d8b66b",
        blue: "#9aa8c7",
        magenta: "#c3a0cf",
        cyan: "#93c5bd",
        white: "#d8cfc0",
        brightBlack: "#9b9182",
        brightRed: "#ef9a8d",
        brightGreen: "#a8d18c",
        brightYellow: "#e6c77e",
        brightBlue: "#b0bbd8",
        brightMagenta: "#d5b4df",
        brightCyan: "#a9d7cf",
        brightWhite: "#fff8ed",
      }
);

export const TerminalPanel = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XtermLike | null>(null);
  const fitRef = useRef<FitAddonLike | null>(null);
  const activeRef = useRef<string | null>(null);
  const outputBufferRef = useRef<Record<string, string>>({});
  const inputQueueRef = useRef<Record<string, string[]>>({});
  const webLineRef = useRef("");
  const mountedRef = useRef(true);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const activeTerminalSessionId = useAppStore((s) => s.activeTerminalSessionId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const openLivePreview = useAppStore((s) => s.openLivePreview);
  const themeMode = useAppStore((s) => s.themeMode);
  const [booting, setBooting] = useState(true);
  const [autoCreating, setAutoCreating] = useState(false);
  const [terminalReady, setTerminalReady] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [detectedUrls, setDetectedUrls] = useState<string[]>([]);
  const autoCreateAttemptedRef = useRef(false);

  const activeSession = terminalSessions.find((session) => session.id === activeTerminalSessionId) ?? null;
  const liveUrl = detectedUrls[0];

  const mirrorTerminalCreated = (session: TerminalSessionInfo) => {
    if (!isDesktop()) return;
    sendClientCommand({
      type: "terminal.mirror.created",
      session_id: session.id,
      pid: session.pid,
      shell: session.shell,
      cwd: session.cwd,
    });
  };

  const mirrorTerminalOutput = (sessionId: string, data: string) => {
    if (!isDesktop() || !data) return;
    const session = useAppStore.getState().terminalSessions.find((item) => item.id === sessionId);
    sendClientCommand({
      type: "terminal.mirror.output",
      session_id: sessionId,
      data,
      pid: session?.pid,
      shell: session?.shell,
      cwd: session?.cwd,
    });
  };

  const mirrorTerminalExit = (sessionId: string, exitCode?: number) => {
    if (!isDesktop()) return;
    sendClientCommand({
      type: "terminal.mirror.exit",
      session_id: sessionId,
      exit_code: exitCode,
    });
  };

  const ensureAgentCommandSession = (activate = false) => {
    const store = useAppStore.getState();
    store.upsertTerminalSession({
      id: AGENT_COMMAND_SESSION_ID,
      shell: "Agent commands",
      cwd: workingDirectory || "",
      status: "running",
      createdAt: Date.now(),
    });
    if (!outputBufferRef.current[AGENT_COMMAND_SESSION_ID]) {
      outputBufferRef.current[AGENT_COMMAND_SESSION_ID] = "Agent command stream\r\n";
    }
    if (activate || !store.activeTerminalSessionId) {
      store.setActiveTerminalSession(AGENT_COMMAND_SESSION_ID);
      activeRef.current = AGENT_COMMAND_SESSION_ID;
      redrawActiveSession();
    }
  };

  useEffect(() => {
    activeRef.current = activeTerminalSessionId;
  }, [activeTerminalSessionId]);

  useEffect(() => {
    mountedRef.current = true;
    let disposed = false;
    Promise.all([import("@xterm/xterm"), import("@xterm/addon-fit")])
      .then(([{ Terminal }, { FitAddon }]) => {
        if (disposed || !containerRef.current) return;
        const isLight = document.documentElement.getAttribute("data-theme") === "light";
        const term = new Terminal({
          fontSize: 13,
          fontFamily: "Cascadia Mono, Consolas, JetBrains Mono, Menlo, monospace",
          lineHeight: 1.12,
          letterSpacing: 0,
          cursorBlink: true,
          allowTransparency: true,
          scrollback: 8000,
          theme: terminalTheme(isLight),
        }) as unknown as XtermWithOptions;
        const fitAddon = new FitAddon() as FitAddonLike;
        term.loadAddon(fitAddon);
        term.open(containerRef.current);
        fitAddon.fit();
        termRef.current = term;
        fitRef.current = fitAddon;
        setTerminalReady(true);
        term.attachCustomKeyEventHandler?.((event: KeyboardEvent) => {
        const isCopy = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c";
          const isExplicitCopy = isCopy && event.shiftKey;
          if (isExplicitCopy) {
            void copyTerminalSelection(term);
            return false;
          }
          if (isCopy && term.hasSelection?.()) {
            void copyTerminalSelection(term);
            return false;
          }
          return true;
        });
        term.onData((input) => {
          const sessionId = activeRef.current;
          if (sessionId) {
            writeToSession(sessionId, input);
            return;
          }
          writeWebFallbackInput(input);
        });
        requestAnimationFrame(() => {
          fitAddon.fit();
          redrawActiveSession();
          term.focus?.();
        });
      })
      .catch((error) => {
        setStatusMessage(`Terminal UI failed to load: ${String(error)}`);
      });

    return () => {
      disposed = true;
      mountedRef.current = false;
      termRef.current?.dispose();
      setTerminalReady(false);
    };
  }, []);

  useEffect(() => {
    if (!termRef.current) return;
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    const term = termRef.current as unknown as XtermWithOptions;
    term.options = {
      ...term.options,
      theme: terminalTheme(isLight),
    };
  }, [themeMode]);

  useEffect(() => {
    const resizeObserver = new ResizeObserver(() => {
      safeFit();
      const sessionId = activeRef.current;
      const term = termRef.current;
      if (!sessionId || !term) return;
      resizeSession(sessionId, term.cols, term.rows);
    });
    if (containerRef.current) resizeObserver.observe(containerRef.current);
    return () => resizeObserver.disconnect();
  }, []);

  useEffect(() => {
    void refreshSessions();
    const ws = getWebSocket();
    const unsub = ws?.subscribe((msg) => {
      const e = msg as {
        type?: string;
        session_id?: string;
        data?: string;
        output?: string;
        command?: string;
        exit_code?: number;
        name?: string;
        args?: { command?: string };
        result_kind?: string;
        status?: string;
        summary?: string;
      };
      if (e.type === "terminal.output" && e.session_id && e.data) {
        appendOutput(e.session_id, e.data);
      } else if (e.type === "terminal.output" && e.output != null) {
        appendOutput("web-fallback", `${e.output}\r\n[exit ${e.exit_code ?? 0}]\r\n$ `);
      } else if (e.type === "terminal.exit" && e.session_id) {
        appendOutput(e.session_id, `\r\n[Process exited with code ${e.exit_code ?? 0}]\r\n`);
      } else if (e.type === "terminal.list") {
        if (mountedRef.current) setBooting(false);
      } else if (e.type === "tool_call" && (e.result_kind === "command" || /run_command|terminal|shell|bash|powershell|cmd/i.test(e.name ?? ""))) {
        ensureAgentCommandSession(true);
        const command = e.args?.command || e.command || e.name || "command";
        appendOutput(AGENT_COMMAND_SESSION_ID, `\r\n$ ${command}\r\n`);
      } else if (e.type === "tool_output_delta" && e.output) {
        ensureAgentCommandSession();
        appendOutput(AGENT_COMMAND_SESSION_ID, toTerminalNewlines(e.output));
      } else if (e.type === "tool_result" && e.result_kind === "command") {
        ensureAgentCommandSession();
        const status = e.status || (e.exit_code && e.exit_code !== 0 ? "failed" : "completed");
        appendOutput(AGENT_COMMAND_SESSION_ID, `\r\n[agent command ${status}]\r\n`);
        const current = useAppStore.getState().terminalSessions.find((session) => session.id === AGENT_COMMAND_SESSION_ID);
        if (current) useAppStore.getState().upsertTerminalSession({ ...current, status: "exited" });
      }
    });

    let desktopDataCleanup: (() => void) | undefined;
    let desktopExitCleanup: (() => void) | undefined;
    if (isDesktop()) {
      const d = desktop();
      desktopDataCleanup = d?.pty.onData(({ sessionId, data }) => {
        appendOutput(sessionId, data);
        mirrorTerminalOutput(sessionId, data);
      }) as (() => void) | undefined;
      desktopExitCleanup = d?.pty.onExit(({ sessionId, exitCode }) => {
        appendOutput(sessionId, `\r\n[Process exited with code ${exitCode}]\r\n`);
        mirrorTerminalExit(sessionId, exitCode);
        const current = useAppStore.getState().terminalSessions.find((session) => session.id === sessionId);
        if (current) useAppStore.getState().upsertTerminalSession({ ...current, status: "exited" });
      }) as (() => void) | undefined;
    }

    return () => {
      unsub?.();
      desktopDataCleanup?.();
      desktopExitCleanup?.();
    };
  }, []);

  useEffect(() => {
    if (!activeTerminalSessionId && terminalSessions.length > 0) {
      useAppStore.getState().setActiveTerminalSession(terminalSessions[0].id);
    }
  }, [activeTerminalSessionId, terminalSessions]);

  useEffect(() => {
    redrawActiveSession();
    requestAnimationFrame(() => {
      safeFit();
      termRef.current?.focus?.();
    });
  }, [activeTerminalSessionId]);

  useEffect(() => {
    if (activeSession && statusMessage === "Starting backend shell...") {
      setStatusMessage("");
    }
  }, [activeSession, statusMessage]);

  useEffect(() => {
    redrawActiveSession();
  }, [statusMessage]);

  useEffect(() => {
    const onVisibilityFit = () => {
      requestAnimationFrame(() => {
        safeFit();
        termRef.current?.focus?.();
      });
    };
    window.addEventListener("focus", onVisibilityFit);
    document.addEventListener("visibilitychange", onVisibilityFit);
    return () => {
      window.removeEventListener("focus", onVisibilityFit);
      document.removeEventListener("visibilitychange", onVisibilityFit);
    };
  }, []);

  useEffect(() => {
    if (!terminalReady || booting || terminalSessions.length > 0 || autoCreateAttemptedRef.current) return;
    if (!termRef.current) return;
    autoCreateAttemptedRef.current = true;
    void createSession();
  }, [terminalReady, booting, terminalSessions.length]);

  const refreshSessions = async () => {
    setBooting(true);
    setStatusMessage("");
    let waitForBackendList = false;
    try {
      if (isDesktop()) {
        const sessions = await ptyList();
        const normalized: TerminalSessionInfo[] = sessions.map((session) => ({
          id: session.sessionId,
          pid: session.pid,
          shell: session.shell,
          cwd: session.cwd,
          status: "running",
        }));
        useAppStore.getState().setTerminalSessions(normalized);
        for (const session of normalized) mirrorTerminalCreated(session);
      } else {
        // Web 浏览器环境禁用后端列表请求，防止异步覆盖 fallback 终端
        const sent = getWebSocket()?.send({ type: "terminal.list" }) ?? false;
        waitForBackendList = sent;
        if (!sent) useAppStore.getState().setTerminalSessions([]);
      }
    } catch (error) {
      setStatusMessage(`Terminal refresh failed: ${String(error)}`);
    } finally {
      if (mountedRef.current && !waitForBackendList) setBooting(false);
    }
  };

  const createSession = async () => {
    if (autoCreating) return;
    setAutoCreating(true);
    setStatusMessage("");
    const cwd = workingDirectory || undefined;
    try {
      if (isDesktop()) {
        const session = await ptySpawn(cwd);
        if (!session) {
          setStatusMessage("Command Runner ready. Type a command and press Enter.");
          if (!outputBufferRef.current["web-fallback"]) {
            outputBufferRef.current["web-fallback"] = "Command Runner. Commands run in the current workspace and are not interactive.\r\n$ ";
          }
          useAppStore.getState().setActiveTerminalSession(null);
          redrawActiveSession();
          requestAnimationFrame(() => termRef.current?.focus?.());
          return;
        }
        useAppStore.getState().upsertTerminalSession({
          id: session.sessionId,
          pid: session.pid,
          shell: session.shell,
          cwd: session.cwd,
          status: "running",
          createdAt: Date.now(),
        });
        mirrorTerminalCreated({
          id: session.sessionId,
          pid: session.pid,
          shell: session.shell,
          cwd: session.cwd,
          status: "running",
          createdAt: Date.now(),
        });
        useAppStore.getState().setActiveTerminalSession(session.sessionId);
        requestAnimationFrame(() => {
          safeFit();
          const term = termRef.current;
          if (term && session.sessionId) {
            resizeSession(session.sessionId, term.cols, term.rows);
          }
          term?.focus?.();
        });
      } else {
        const sent = getWebSocket()?.send({ type: "terminal.create", cwd }) ?? false;
        if (sent) {
          setStatusMessage("Starting backend shell...");
        } else {
          setStatusMessage("Command Runner ready. Type a command and press Enter.");
          useAppStore.getState().setActiveTerminalSession(null);
          if (!outputBufferRef.current["web-fallback"]) {
            outputBufferRef.current["web-fallback"] = "Command Runner. Commands run in the current workspace and are not interactive.\r\n$ ";
          }
          redrawActiveSession();
        }
        requestAnimationFrame(() => {
          safeFit();
          termRef.current?.focus?.();
        });
      }
    } catch (error) {
      setStatusMessage(`Terminal start failed: ${String(error)}`);
      useAppStore.getState().setActiveTerminalSession(null);
      redrawActiveSession();
    } finally {
      if (mountedRef.current) setAutoCreating(false);
    }
  };

  const killSession = (sessionId: string) => {
    delete outputBufferRef.current[sessionId];
    if (sessionId === AGENT_COMMAND_SESSION_ID) {
      useAppStore.getState().removeTerminalSession(sessionId);
      return;
    }
    if (isDesktop()) {
      void ptyKill(sessionId);
      useAppStore.getState().removeTerminalSession(sessionId);
    } else {
      sendClientCommand({ type: "terminal.kill", session_id: sessionId });
    }
  };

  const restartSession = async (sessionId: string) => {
    killSession(sessionId);
    await createSession();
  };

  const appendOutput = (sessionId: string, data: string) => {
    outputBufferRef.current[sessionId] = `${outputBufferRef.current[sessionId] ?? ""}${data}`.slice(-80_000);
    const found = Array.from(data.matchAll(DEV_SERVER_URL_RE), (match) => normalizeDetectedUrl(match[0]));
    if (found.length > 0) {
      const store = useAppStore.getState();
      for (const url of found) {
        const port = portFromUrl(url);
        if (port != null) {
          store.addPreviewServer({ port, url, name: `:${port}`, framework: "terminal" });
        }
      }
      setDetectedUrls((current) => {
        const next = [...found, ...current.filter((url) => !found.includes(url))];
        return next.slice(0, 5);
      });
    }
    if (activeRef.current === sessionId || (!activeRef.current && sessionId === "web-fallback")) termRef.current?.write(data);
  };

  const runWebFallbackCommand = (command: string) => {
    const trimmed = command.trim();
    if (!trimmed) {
      appendOutput("web-fallback", "\r\n$ ");
      return;
    }
    appendOutput("web-fallback", "\r\n");
    sendClientCommand({ type: "terminal.exec", command: trimmed, cwd: workingDirectory || undefined });
  };

  const writeWebFallbackInput = (input: string) => {
    const term = termRef.current;
    if (!term) return;
    for (const char of input) {
      if (char === "\r" || char === "\n") {
        const command = webLineRef.current;
        webLineRef.current = "";
        runWebFallbackCommand(command);
        continue;
      }
      if (char === "\u007f" || char === "\b") {
        if (webLineRef.current.length > 0) {
          webLineRef.current = webLineRef.current.slice(0, -1);
          term.write("\b \b");
        }
        continue;
      }
      if (char >= " ") {
        webLineRef.current += char;
        term.write(char);
      }
    }
  };

  const redrawActiveSession = () => {
    const term = termRef.current;
    if (!term) return;
    term.clear();
    safeFit();
    const sessionId = activeRef.current;
    if (!sessionId) {
      const fallbackOutput = outputBufferRef.current["web-fallback"];
      if (fallbackOutput) {
        term.write(fallbackOutput);
        return;
      }
      const message = statusMessage || (
        isDesktop()
          ? (booting || autoCreating ? "Starting terminal..." : "No terminal session. Click + to start one.")
          : "Command Runner ready. Type a command and press Enter."
      );
      term.writeln(message);
      if (!isDesktop()) term.write("$ ");
      return;
    }
    const buffered = outputBufferRef.current[sessionId] ?? "";
    if (buffered) {
      term.write(buffered);
    }
    const queued = inputQueueRef.current[sessionId] ?? [];
    if (queued.length > 0) {
      inputQueueRef.current[sessionId] = [];
      for (const chunk of queued) writeToSession(sessionId, chunk);
    }
  };

  const writeToSession = (sessionId: string, data: string) => {
    const session = useAppStore.getState().terminalSessions.find((item) => item.id === sessionId);
    if (!session || session.status === "exited") {
      appendOutput("web-fallback", statusMessage ? "" : "\r\nTerminal session is not running. Switching to Command Runner.\r\n$ ");
      activeRef.current = null;
      useAppStore.getState().setActiveTerminalSession(null);
      writeWebFallbackInput(data);
      return;
    }
    if (isDesktop()) void ptyWrite(sessionId, data);
    else sendClientCommand({ type: "terminal.input", session_id: sessionId, data });
  };

  const safeFit = () => {
    try {
      fitRef.current?.fit();
    } catch {
      // xterm fit can throw while the panel is hidden or has zero size.
    }
  };

  const resizeSession = (sessionId: string, cols: number, rows: number) => {
    if (isDesktop()) void ptyResize(sessionId, cols, rows);
    else sendClientCommand({ type: "terminal.resize", session_id: sessionId, cols, rows });
  };

  const copyTerminalSelection = async (term = termRef.current) => {
    const text = term?.getSelection?.() ?? "";
    if (!text) return;
    await navigator.clipboard?.writeText(text);
    term?.clearSelection?.();
    term?.focus?.();
  };

  return (
    <div
      onMouseDown={() => termRef.current?.focus?.()}
      onContextMenu={(event) => {
        if (termRef.current?.hasSelection?.()) {
          event.preventDefault();
          void copyTerminalSelection();
        }
      }}
      className="h-full w-full flex flex-col overflow-hidden p-2 gap-2 border-0 rounded-none shadow-none"
      style={{ background: "var(--surface-base)" }}
    >
      <div
        className="flex items-center gap-1.5 p-0 border-b-0 bg-transparent min-h-7"
        style={{ fontSize: "var(--text-sm)", color: "var(--text-primary)" }}
      >
        <button
          type="button"
          onClick={() => void createSession()}
          className="w-6 h-6 inline-flex items-center justify-center border-0 bg-transparent cursor-pointer p-0"
          style={{ color: "var(--text-primary)", borderRadius: "var(--radius-sm, 6px)" }}
          title="New terminal"
          aria-label="New terminal"
        >
          <Plus size={14} />
        </button>
        {liveUrl && (
          <button
            type="button"
            title={`Open ${liveUrl} in Preview Pane`}
            aria-label={`Open ${liveUrl} in Preview Pane`}
            onClick={() => {
              openLivePreview(liveUrl);
              getWebSocket()?.send({ type: "preview.navigate", url: liveUrl });
            }}
            className="inline-flex items-center gap-1.5 max-w-52 h-7 px-2 cursor-pointer"
            style={{
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-sm, 4px)",
              background: "var(--surface-raised)",
              color: "var(--text-primary)",
              fontSize: "var(--text-xs)",
              fontFamily: "var(--font-mono)",
            }}
          >
            <Globe size={13} />
            <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
              {liveUrl.replace(/^https?:\/\//, "")}
            </span>
            <ExternalLink size={12} />
          </button>
        )}
        <IconButton label="Refresh terminal list" onClick={() => void refreshSessions()}>
          <RotateCcw size={13} />
        </IconButton>
        <IconButton label="Copy selected terminal text" onClick={() => void copyTerminalSelection()}>
          <Copy size={13} />
        </IconButton>
        <IconButton label="Clear terminal" onClick={() => termRef.current?.clear()}>
          <Square size={12} />
        </IconButton>
        {activeSession && (
          <IconButton
            label={activeSession.status === "exited" ? "Restart terminal" : "Kill terminal"}
            onClick={() => activeSession.status === "exited" ? void restartSession(activeSession.id) : killSession(activeSession.id)}
          >
            {activeSession.status === "exited" ? <RotateCcw size={13} /> : <Trash2 size={13} />}
          </IconButton>
        )}
      </div>
      <div
        ref={containerRef}
        className="flex-1 w-full min-h-0 p-0.5 overflow-hidden"
        style={{
          background: terminalTheme(themeMode === "light" || document.documentElement.getAttribute("data-theme") === "light").background,
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-sm, 4px)",
        }}
      />
    </div>
  );
};

const IconButton = ({
  children,
  label,
  onClick,
}: {
  children: React.ReactNode;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    onClick={onClick}
    onMouseDown={(event) => event.stopPropagation()}
    title={label}
    aria-label={label}
    className="bg-transparent border border-transparent p-0 min-w-7 h-7 cursor-pointer inline-flex items-center justify-center"
    style={{
      borderRadius: "var(--radius-sm, 6px)",
      fontSize: "var(--text-xs)",
      color: "var(--text-muted)",
    }}
  >
    {children}
  </button>
);
