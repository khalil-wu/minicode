import { useEffect, useRef, useState } from "react";
import { Copy, ExternalLink, Globe, Plus, RotateCcw, Square, Trash2 } from "lucide-react";
import "@xterm/xterm/css/xterm.css";
import { getWebSocket } from "../hooks/useWebSocket";
import { useAppStore } from "../stores";
import { desktop, isDesktop, ptyAckExit, ptyClear, ptyKill, ptyList, ptyResize, ptyRestart, ptySnapshot, ptySpawn, ptyWrite } from "../desktop/runtime";
import type { TerminalSessionInfo } from "../stores/types";
import { commandResultSucceeded, sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { openWebInBrowser } from "../chat/openWebInBrowser";
import {
  NEW_TERMINAL_SESSION_EVENT,
  consumeNewTerminalSessionRequest,
  hasPendingNewTerminalSessionRequest,
} from "./terminalRequests";

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

type XtermWithOptions = XtermLike & {
  options: {
    theme?: Record<string, string>;
    fontSize?: number;
  };
};

const DEV_SERVER_URL_RE = /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:[/?#][^\s'"<>]*)?/gi;
const TERMINAL_OUTPUT_BUFFER_CHARS = 80_000;

export const mergeTerminalOutputSnapshot = (snapshot: string, live: string): string => {
  if (!snapshot) return live.slice(-TERMINAL_OUTPUT_BUFFER_CHARS);
  if (!live) return snapshot.slice(-TERMINAL_OUTPUT_BUFFER_CHARS);
  if (snapshot.endsWith(live)) return snapshot.slice(-TERMINAL_OUTPUT_BUFFER_CHARS);
  if (live.endsWith(snapshot)) return live.slice(-TERMINAL_OUTPUT_BUFFER_CHARS);
  const overlapLimit = Math.min(snapshot.length, live.length, 4096);
  let overlap = overlapLimit;
  while (overlap > 0 && snapshot.slice(-overlap) !== live.slice(0, overlap)) overlap -= 1;
  return `${snapshot}${live.slice(overlap)}`.slice(-TERMINAL_OUTPUT_BUFFER_CHARS);
};

export const mergeTerminalOutputByCursor = (
  snapshot: string,
  snapshotStartCursor: number | undefined,
  snapshotEndCursor: number | undefined,
  live: string,
  liveEndCursor: number | undefined,
): { output: string; endCursor?: number } => {
  if (snapshotStartCursor == null || snapshotEndCursor == null || liveEndCursor == null) {
    return { output: mergeTerminalOutputSnapshot(snapshot, live), endCursor: snapshotEndCursor ?? liveEndCursor };
  }
  const liveStartCursor = liveEndCursor - live.length;
  if (liveEndCursor <= snapshotEndCursor) {
    return { output: snapshot.slice(-TERMINAL_OUTPUT_BUFFER_CHARS), endCursor: snapshotEndCursor };
  }
  const liveSuffixOffset = Math.max(0, snapshotEndCursor - liveStartCursor);
  return {
    output: `${snapshot}${live.slice(liveSuffixOffset)}`.slice(-TERMINAL_OUTPUT_BUFFER_CHARS),
    endCursor: liveEndCursor,
  };
};

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

export const terminalSessionLabel = (
  session: TerminalSessionInfo,
  index: number,
  sessionCount: number,
): string => {
  const name = session.shell?.split(/[/\\]/).pop()?.replace(/\.(exe|cmd|ps1)$/i, "") || "shell";
  const indexedName = sessionCount > 1 ? `${name} ${index + 1}` : name;
  return session.terminalMode === "pipe" ? `${indexedName}（基础）` : indexedName;
};

export const terminalExitCodeLabel = (exitCode: unknown): string =>
  typeof exitCode === "number" && Number.isSafeInteger(exitCode)
    ? String(exitCode)
    : "unknown";

const terminalStatusLabel = (status: TerminalSessionInfo["status"]): string =>
  status === "exited" ? "已退出" : "运行中";

/**
 * xterm.js 走 canvas 渲染，读不到 CSS 变量，故此处必须是字面量 —— 但取值不是
 * 随手挑的：全部由 tokens.css 的 OKLCH 刻度换算而来，保证终端与它所嵌入的
 * 面板（容器为 --surface-base）同色相、同明度，不再出现"冷灰外壳 + 暖棕终端"
 * 的接缝。
 *   background/foreground = --surface-base / --text-secondary
 *   cursor                = --accent-primary
 *   ANSI 六色             = --state-* 同色相(28/142/78/240/305/195)，
 *                           暗色 L=0.74 C=0.13、亮档 L=0.845
 * 例外：dim 档（暗色 brightBlack、亮色 white）按刻度取值只有 ~2.6:1，读不清，
 * 故各提一档到 L=0.62 / L=0.56，使其 ≥4.5:1。全部 16 色均已验证达 AA。
 * 改动 tokens.css 的表面或状态色时，这里需要同步换算并重验对比度。
 */
/* Font: resolve from the same tokens every other code surface uses so the
 * terminal follows --font-mono and the Appearance-tab code zoom setting. */
const terminalFontFamily = (): string => {
  const stack = getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim();
  return stack || '"JetBrains Mono", Consolas, monospace';
};

const TERMINAL_BASE_FONT_SIZE = 13;

const terminalFontSize = (): number => {
  const codeFontPx = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--code-font-size"));
  if (!Number.isFinite(codeFontPx) || codeFontPx <= 0) return TERMINAL_BASE_FONT_SIZE;
  // Terminal base is 13px against the 15px code base; keep that ratio under zoom.
  return Math.max(9, Math.round((TERMINAL_BASE_FONT_SIZE * codeFontPx) / 15));
};

const terminalTheme = (isLight: boolean): Record<string, string> => (
  isLight
    ? {
        background: "#ffffff",
        foreground: "#191b1d",
        cursor: "#1467c2",
        selectionBackground: "#cbdbed",
        black: "#121416",
        red: "#b33830",
        green: "#207e18",
        yellow: "#995800",
        blue: "#0070ba",
        magenta: "#7f4bb1",
        cyan: "#008285",
        white: "#737577",
        brightBlack: "#595b5e",
        brightRed: "#941d19",
        brightGreen: "#006400",
        brightYellow: "#7e4000",
        brightBlue: "#00569c",
        brightMagenta: "#663294",
        brightCyan: "#00686b",
        brightWhite: "#040405",
      }
    : {
        background: "#050606",
        foreground: "#e6e8ea",
        cursor: "#0f92f7",
        selectionBackground: "#2b343d",
        black: "#030304",
        red: "#f2897c",
        green: "#79bf72",
        yellow: "#d7a03d",
        blue: "#52b5f4",
        magenta: "#be95ec",
        cyan: "#00c4c4",
        white: "#bcbec0",
        brightBlack: "#83868a",
        brightRed: "#ffafa2",
        brightGreen: "#a1df99",
        brightYellow: "#f5c372",
        brightBlue: "#82d6ff",
        brightMagenta: "#ddb9ff",
        brightCyan: "#60e4e3",
        brightWhite: "#fcfdff",
      }
);

export const TerminalPanel = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XtermLike | null>(null);
  const fitRef = useRef<FitAddonLike | null>(null);
  const activeRef = useRef<string | null>(null);
  const outputBufferRef = useRef<Record<string, string>>({});
  const outputCursorRef = useRef<Record<string, number>>({});
  const hydratedSnapshotRef = useRef<Set<string>>(new Set());
  const refreshEpochRef = useRef(0);
  const inputQueueRef = useRef<Record<string, string[]>>({});
  const webLineRef = useRef("");
  const mountedRef = useRef(true);
  const createSessionRef = useRef<() => Promise<void>>(async () => {});
  const refreshSessionsRef = useRef<() => Promise<void>>(async () => {});
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const activeTerminalSessionId = useAppStore((s) => s.activeTerminalSessionId);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const terminalSnapshots = useAppStore((s) => s.terminalSnapshots);
  const [booting, setBooting] = useState(true);
  const [autoCreating, setAutoCreating] = useState(false);
  const creatingRef = useRef(false);
  const [terminalReady, setTerminalReady] = useState(false);
  const [statusMessage, setStatusMessage] = useState("");
  const [terminatingSessionIds, setTerminatingSessionIds] = useState<Set<string>>(() => new Set());
  const terminatingSessionIdsRef = useRef(new Set<string>());
  const [detectedUrls, setDetectedUrls] = useState<string[]>([]);
  const autoCreateAttemptedRef = useRef(false);

  const activeSession = terminalSessions.find((session) => (
    session.id === activeTerminalSessionId && session.conversationId === conversationId
  )) ?? null;
  const liveUrl = detectedUrls[0];

  const mirrorTerminalCreated = (session: TerminalSessionInfo) => {
    if (!isDesktop() || !session.conversationId) return;
    sendClientCommand({
      type: "terminal.mirror.created",
      conversation_id: session.conversationId,
      session_id: session.id,
      pid: session.pid,
      shell: session.shell,
      cwd: session.cwd,
      is_alive: session.status !== "exited",
    });
  };

  const mirrorTerminalOutput = (sessionId: string, conversationOwner: string, data: string) => {
    if (!isDesktop() || !conversationOwner || !data) return;
    const session = useAppStore.getState().terminalSessions.find((item) => item.id === sessionId);
    sendClientCommand({
      type: "terminal.mirror.output",
      conversation_id: conversationOwner,
      session_id: sessionId,
      data,
      pid: session?.pid,
      shell: session?.shell,
      cwd: session?.cwd,
    });
  };

  const mirrorTerminalExit = (sessionId: string, conversationOwner: string, exitCode?: number) => {
    if (!isDesktop() || !conversationOwner) return;
    sendClientCommand({
      type: "terminal.mirror.exit",
      conversation_id: conversationOwner,
      session_id: sessionId,
      exit_code: exitCode,
    });
  };

  const removeMirroredTerminal = (sessionId: string, conversationOwner: string) => {
    if (!isDesktop() || !conversationOwner) return;
    const activeConversationId = useAppStore.getState().conversationId || "";
    if (activeConversationId !== conversationOwner) return;
    // The Electron process owns and kills the real PTY. The backend only owns
    // its reconnectable mirror, so delete that mirror through the existing
    // terminal.kill lifecycle after the local PTY has been removed. Without
    // this, deleted/restarted PowerShell tabs remain visible to agent tools as
    // stale external terminal sessions.
    sendClientCommand(
      {
        type: "terminal.kill",
        session_id: sessionId,
        conversation_id: conversationOwner,
        workspace_root: useAppStore.getState().workingDirectory || undefined,
      },
      { silent: true },
    );
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
        const isLight = useAppStore.getState().resolvedTheme === "light";
        const term = new Terminal({
          fontSize: terminalFontSize(),
          fontFamily: terminalFontFamily(),
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
        setStatusMessage(`终端界面加载失败：${String(error)}`);
      });

    return () => {
      disposed = true;
      mountedRef.current = false;
      refreshEpochRef.current += 1;
      termRef.current?.dispose();
      setTerminalReady(false);
    };
  }, []);

  useEffect(() => {
    if (!termRef.current) return;
    const isLight = resolvedTheme === "light";
    const term = termRef.current as unknown as XtermWithOptions;
    term.options.theme = terminalTheme(isLight);
  }, [resolvedTheme]);

  // Follow the Appearance-tab code zoom: xterm needs a numeric px, so re-read
  // the scaled token and re-fit the grid.
  const codeTextScale = useAppStore((s) => s.codeTextScale);
  useEffect(() => {
    if (!termRef.current) return;
    const term = termRef.current as unknown as XtermWithOptions;
    term.options.fontSize = terminalFontSize();
    safeFit();
  }, [codeTextScale]);

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
        conversation_id?: string;
      };
      const eventOwner = e.conversation_id?.trim() || "";
      const activeOwner = useAppStore.getState().conversationId || "";
      if (
        e.type?.startsWith("terminal.")
        && (!eventOwner || !activeOwner || eventOwner !== activeOwner)
      ) return;
      if (e.type === "terminal.output" && e.session_id && e.data) {
        appendOutput(e.session_id, e.data);
      } else if (e.type === "terminal.output" && e.output != null) {
        appendOutput("web-fallback", `${e.output}\r\n[exit ${terminalExitCodeLabel(e.exit_code)}]\r\n$ `);
      } else if (e.type === "terminal.exit" && e.session_id) {
        appendOutput(e.session_id, `\r\n[Process exited with code ${terminalExitCodeLabel(e.exit_code)}]\r\n`);
      } else if (e.type === "terminal.killed" && e.session_id) {
        delete outputBufferRef.current[e.session_id];
        delete outputCursorRef.current[e.session_id];
        hydratedSnapshotRef.current.delete(e.session_id);
      } else if (e.type === "terminal.list") {
        if (mountedRef.current) setBooting(false);
      }
    });

    let desktopDataCleanup: (() => void) | undefined;
    let desktopExitCleanup: (() => void) | undefined;
    if (isDesktop()) {
      const d = desktop();
      desktopDataCleanup = d?.pty.onData(({ sessionId, conversationId: owner, data, startCursor, endCursor }) => {
        if (!owner) return;
        const acceptedCursor = outputCursorRef.current[sessionId];
        if (endCursor != null && acceptedCursor != null && endCursor <= acceptedCursor) return;
        appendOutput(sessionId, data, startCursor, endCursor);
        mirrorTerminalOutput(sessionId, owner, data);
      }) as (() => void) | undefined;
      desktopExitCleanup = d?.pty.onExit(({ sessionId, conversationId: owner, exitCode }) => {
        if (!owner) return;
        mirrorTerminalExit(sessionId, owner, exitCode);
        const current = useAppStore.getState().terminalSessions.find((session) => session.id === sessionId);
        if (current?.conversationId === owner) {
          useAppStore.getState().upsertTerminalSession({ ...current, status: "exited", exitCode, exitedAt: Date.now() });
        }
      }) as (() => void) | undefined;
    }

    return () => {
      unsub?.();
      desktopDataCleanup?.();
      desktopExitCleanup?.();
    };
  }, []);

  useEffect(() => {
    autoCreateAttemptedRef.current = false;
    // restoreWorkbenchState has already projected the target conversation's
    // cached sessions and preferred terminal. Keep them visible until the
    // authoritative list for this owner arrives and reconciles the cache.
    void refreshSessionsRef.current();
  }, [conversationId]);

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
    if (activeSession && statusMessage === "正在启动后端 Shell...") {
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
    if (hasPendingNewTerminalSessionRequest()) return;
    if (!termRef.current) return;
    autoCreateAttemptedRef.current = true;
    void createSession();
  }, [terminalReady, booting, terminalSessions.length]);

  const refreshSessions = async () => {
    const refreshEpoch = ++refreshEpochRef.current;
    const ownerConversationId = useAppStore.getState().conversationId || "";
    setBooting(true);
    setStatusMessage("");
    try {
      if (!ownerConversationId) {
        useAppStore.getState().setTerminalSessions([]);
        setStatusMessage("请先选择会话，再打开终端。");
        return;
      }
      if (isDesktop()) {
        const listedSessions = await ptyList(ownerConversationId);
        const sessions = await Promise.all(listedSessions.map(async (session) => (
          await ptySnapshot(session.sessionId, ownerConversationId, TERMINAL_OUTPUT_BUFFER_CHARS) ?? session
        )));
        if (
          !mountedRef.current
          || refreshEpoch !== refreshEpochRef.current
          || useAppStore.getState().conversationId !== ownerConversationId
        ) return;
        for (const session of sessions) {
          if (!session.output) continue;
          const merged = mergeTerminalOutputByCursor(
            session.output,
            session.outputStartCursor,
            session.outputEndCursor,
            outputBufferRef.current[session.sessionId] ?? "",
            outputCursorRef.current[session.sessionId],
          );
          outputBufferRef.current[session.sessionId] = merged.output;
          if (merged.endCursor != null) outputCursorRef.current[session.sessionId] = merged.endCursor;
        }
        const normalized: TerminalSessionInfo[] = sessions.map((session) => ({
          id: session.sessionId,
          conversationId: session.conversationId,
          pid: session.pid,
          shell: session.shell,
          cwd: session.cwd,
          status: session.isAlive === false ? "exited" : "running",
          exitCode: session.exitCode,
          exitedAt: session.exitedAt,
          terminalMode: "pty",
        }));
        useAppStore.getState().setTerminalSessions(
          normalized.filter((session) => session.conversationId === ownerConversationId),
        );
        for (const session of normalized) mirrorTerminalCreated(session);
        redrawActiveSession();
      } else {
        const result = await sendClientCommandAwaitResult(
          {
            type: "terminal.list",
            conversation_id: ownerConversationId,
            workspace_root: useAppStore.getState().workingDirectory || undefined,
          },
          "terminal.list",
        );
        if (!commandResultSucceeded(result)) {
          useAppStore.getState().setTerminalSessions([]);
          setStatusMessage(result.message || "刷新终端失败。");
        }
      }
    } catch (error) {
      setStatusMessage(`刷新终端失败：${String(error)}`);
    } finally {
      if (mountedRef.current && refreshEpoch === refreshEpochRef.current) setBooting(false);
    }
  };
  refreshSessionsRef.current = refreshSessions;

  const createSession = async () => {
    if (creatingRef.current) return;
    const ownerConversationId = useAppStore.getState().conversationId || "";
    if (!ownerConversationId) {
      setStatusMessage("请先选择会话，再打开终端。");
      return;
    }
    creatingRef.current = true;
    setAutoCreating(true);
    setStatusMessage("");
    const cwd = workingDirectory || undefined;
    try {
      if (isDesktop()) {
        const session = await ptySpawn(cwd, ownerConversationId);
        if (!session) {
          setStatusMessage("命令运行器已就绪。输入命令后按 Enter。");
          if (!outputBufferRef.current["web-fallback"]) {
            outputBufferRef.current["web-fallback"] = "命令运行器。命令在当前工作区运行，不支持交互式操作。\r\n$ ";
          }
          useAppStore.getState().setActiveTerminalSession(null);
          redrawActiveSession();
          requestAnimationFrame(() => termRef.current?.focus?.());
          return;
        }
        const terminalSession: TerminalSessionInfo = {
          id: session.sessionId,
          conversationId: session.conversationId,
          pid: session.pid,
          shell: session.shell,
          cwd: session.cwd,
          status: "running",
          createdAt: Date.now(),
          terminalMode: "pty",
        };
        mirrorTerminalCreated(terminalSession);
        if (useAppStore.getState().conversationId !== ownerConversationId) return;
        useAppStore.getState().upsertTerminalSession(terminalSession);
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
        const result = await sendClientCommandAwaitResult(
          {
            type: "terminal.create",
            cwd,
            conversation_id: ownerConversationId,
            workspace_root: useAppStore.getState().workingDirectory || undefined,
          },
          "terminal.create",
        );
        if (commandResultSucceeded(result)) {
          setStatusMessage("正在启动后端 Shell...");
        } else {
          setStatusMessage(result.message || "命令运行器已就绪。输入命令后按 Enter。");
          useAppStore.getState().setActiveTerminalSession(null);
          if (!outputBufferRef.current["web-fallback"]) {
            outputBufferRef.current["web-fallback"] = "命令运行器。命令在当前工作区运行，不支持交互式操作。\r\n$ ";
          }
          redrawActiveSession();
        }
        requestAnimationFrame(() => {
          safeFit();
          termRef.current?.focus?.();
        });
      }
    } catch (error) {
      setStatusMessage(`启动终端失败：${String(error)}`);
      useAppStore.getState().setActiveTerminalSession(null);
      redrawActiveSession();
    } finally {
      creatingRef.current = false;
      if (mountedRef.current) setAutoCreating(false);
    }
  };
  createSessionRef.current = createSession;

  useEffect(() => {
    if (!terminalReady || booting) return;
    const openRequestedTerminal = () => {
      if (!consumeNewTerminalSessionRequest()) return;
      void createSessionRef.current();
    };
    openRequestedTerminal();
    window.addEventListener(NEW_TERMINAL_SESSION_EVENT, openRequestedTerminal);
    return () => window.removeEventListener(NEW_TERMINAL_SESSION_EVENT, openRequestedTerminal);
  }, [terminalReady, booting]);

  const killSession = async (sessionId: string): Promise<boolean> => {
    if (terminatingSessionIdsRef.current.has(sessionId)) return false;
    terminatingSessionIdsRef.current.add(sessionId);
    setTerminatingSessionIds((current) => new Set(current).add(sessionId));
    try {
      if (isDesktop()) {
        const session = useAppStore.getState().terminalSessions.find((item) => item.id === sessionId);
        if (!session?.conversationId) return false;
        setStatusMessage("");
        try {
          const removed = session.status === "exited"
            ? await ptyAckExit(sessionId, session.conversationId)
            : await ptyKill(sessionId, session.conversationId);
          if (!removed) {
            setStatusMessage("无法删除终端，因为该会话已不属于当前对话。");
            await refreshSessionsRef.current();
            return false;
          }
          delete outputBufferRef.current[sessionId];
          delete outputCursorRef.current[sessionId];
          hydratedSnapshotRef.current.delete(sessionId);
          useAppStore.getState().removeTerminalSession(sessionId);
          removeMirroredTerminal(sessionId, session.conversationId);
          return true;
        } catch (error) {
          setStatusMessage(`停止终端失败：${String(error)}`);
          return false;
        }
      }
      const state = useAppStore.getState();
      try {
        const result = await sendClientCommandAwaitResult(
          {
            type: "terminal.kill",
            session_id: sessionId,
            conversation_id: state.conversationId || undefined,
            workspace_root: state.workingDirectory || undefined,
          },
          "terminal.kill",
        );
        if (!commandResultSucceeded(result)) {
          setStatusMessage(result.message || "停止终端失败。");
          return false;
        }
        return true;
      } catch (error) {
        setStatusMessage(`停止终端失败：${String(error)}`);
        return false;
      }
    } finally {
      terminatingSessionIdsRef.current.delete(sessionId);
      setTerminatingSessionIds((current) => {
        const next = new Set(current);
        next.delete(sessionId);
        return next;
      });
    }
  };

  const restartSession = async (sessionId: string) => {
    if (!isDesktop()) {
      setStatusMessage("");
      try {
        const result = await sendClientCommandAwaitResult(
          {
            type: "terminal.restart",
            session_id: sessionId,
            conversation_id: useAppStore.getState().conversationId || undefined,
            workspace_root: useAppStore.getState().workingDirectory || undefined,
          },
          "terminal.restart",
        );
        if (!commandResultSucceeded(result)) {
          setStatusMessage(result.message || "重新启动终端失败。");
          return;
        }
        delete outputBufferRef.current[sessionId];
        delete outputCursorRef.current[sessionId];
        hydratedSnapshotRef.current.delete(sessionId);
      } catch (error) {
        setStatusMessage(`重新启动终端失败：${String(error)}`);
      }
      return;
    }
    const session = useAppStore.getState().terminalSessions.find((item) => item.id === sessionId);
    if (!session?.conversationId) return;
    setStatusMessage("");
    const replacement = await ptyRestart(sessionId, session.conversationId);
    if (!replacement) {
      setStatusMessage("重新启动终端失败。请新建终端后继续。");
      return;
    }
    delete outputBufferRef.current[sessionId];
    delete outputCursorRef.current[sessionId];
    hydratedSnapshotRef.current.delete(sessionId);
    removeMirroredTerminal(sessionId, session.conversationId);
    const terminalSession: TerminalSessionInfo = {
      id: replacement.sessionId,
      conversationId: replacement.conversationId,
      pid: replacement.pid,
      shell: replacement.shell,
      cwd: replacement.cwd,
      status: "running",
      createdAt: Date.now(),
      terminalMode: "pty",
    };
    mirrorTerminalCreated(terminalSession);
    const store = useAppStore.getState();
    store.removeTerminalSession(sessionId);
    if (store.conversationId !== session.conversationId) return;
    store.upsertTerminalSession(terminalSession);
    store.setActiveTerminalSession(terminalSession.id);
  };

  const appendOutput = (sessionId: string, data: string, startCursor?: number, endCursor?: number) => {
    let appendedData = data;
    let redrawRequired = false;
    const currentCursor = outputCursorRef.current[sessionId];
    if (startCursor != null && endCursor != null) {
      if (currentCursor != null && endCursor <= currentCursor) return;
      if (currentCursor != null && startCursor < currentCursor) {
        appendedData = data.slice(Math.max(0, currentCursor - startCursor));
      }
      outputCursorRef.current[sessionId] = endCursor;
      outputBufferRef.current[sessionId] = `${outputBufferRef.current[sessionId] ?? ""}${appendedData}`.slice(-TERMINAL_OUTPUT_BUFFER_CHARS);
    } else if (hydratedSnapshotRef.current.has(sessionId)) {
      const currentOutput = outputBufferRef.current[sessionId] ?? "";
      const mergedOutput = mergeTerminalOutputSnapshot(currentOutput, data);
      outputBufferRef.current[sessionId] = mergedOutput;
      appendedData = mergedOutput.startsWith(currentOutput)
        ? mergedOutput.slice(currentOutput.length)
        : "";
      redrawRequired = !mergedOutput.startsWith(currentOutput);
      hydratedSnapshotRef.current.delete(sessionId);
    } else {
      outputBufferRef.current[sessionId] = `${outputBufferRef.current[sessionId] ?? ""}${appendedData}`.slice(-TERMINAL_OUTPUT_BUFFER_CHARS);
    }
    const found = Array.from(appendedData.matchAll(DEV_SERVER_URL_RE), (match) => normalizeDetectedUrl(match[0]));
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
    if (redrawRequired && activeRef.current === sessionId) {
      redrawActiveSession();
    } else if (activeRef.current === sessionId || (!activeRef.current && sessionId === "web-fallback")) {
      termRef.current?.write(appendedData);
    }
  };

  const runWebFallbackCommand = (command: string) => {
    const trimmed = command.trim();
    if (!trimmed) {
      appendOutput("web-fallback", "\r\n$ ");
      return;
    }
    appendOutput("web-fallback", "\r\n");
    sendClientCommand({
      type: "terminal.exec",
      command: trimmed,
      cwd: workingDirectory || undefined,
      conversation_id: conversationId || undefined,
      workspace_root: workingDirectory || undefined,
    });
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
          ? (booting || autoCreating ? "正在启动终端..." : "暂无终端会话，点击 + 新建。")
          : "命令运行器已就绪。输入命令后按 Enter。"
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

  useEffect(() => {
    if (!terminalReady) return;
    let activeSnapshotWasApplied = false;
    for (const snapshot of Object.values(terminalSnapshots)) {
      if (snapshot.conversationId !== conversationId) continue;
      if (!snapshot.output) continue;
      const current = outputBufferRef.current[snapshot.id] ?? "";
      outputBufferRef.current[snapshot.id] = mergeTerminalOutputSnapshot(snapshot.output, current);
      hydratedSnapshotRef.current.add(snapshot.id);
      if (activeRef.current === snapshot.id) activeSnapshotWasApplied = true;
    }
    if (activeSnapshotWasApplied) redrawActiveSession();
  }, [terminalSnapshots, terminalReady, conversationId]);

  const writeToSession = (sessionId: string, data: string) => {
    const session = useAppStore.getState().terminalSessions.find((item) => item.id === sessionId);
    if (!session || session.status === "exited") {
      appendOutput("web-fallback", statusMessage ? "" : "\r\n终端会话未运行，已切换到命令运行器。\r\n$ ");
      activeRef.current = null;
      useAppStore.getState().setActiveTerminalSession(null);
      writeWebFallbackInput(data);
      return;
    }
    if (isDesktop()) void ptyWrite(sessionId, data, session.conversationId);
    else sendClientCommand({
      type: "terminal.input",
      session_id: sessionId,
      data,
      conversation_id: session.conversationId,
      workspace_root: useAppStore.getState().workingDirectory || undefined,
    });
  };

  const safeFit = () => {
    try {
      fitRef.current?.fit();
    } catch {
      // xterm fit can throw while the panel is hidden or has zero size.
    }
  };

  const resizeSession = (sessionId: string, cols: number, rows: number) => {
    if (isDesktop()) {
      const session = useAppStore.getState().terminalSessions.find((item) => item.id === sessionId);
      if (session?.conversationId) void ptyResize(sessionId, cols, rows, session.conversationId);
    } else {
      const state = useAppStore.getState();
      sendClientCommand({
        type: "terminal.resize",
        session_id: sessionId,
        cols,
        rows,
        conversation_id: state.conversationId || undefined,
        workspace_root: state.workingDirectory || undefined,
      });
    }
  };

  const copyTerminalSelection = async (term = termRef.current) => {
    const text = term?.getSelection?.() ?? "";
    if (!text) return;
    await navigator.clipboard?.writeText(text);
    term?.clearSelection?.();
    term?.focus?.();
  };

  const clearActiveTerminal = async () => {
    const state = useAppStore.getState();
    const sessionId = state.activeTerminalSessionId;
    const session = state.terminalSessions.find((item) => item.id === sessionId);
    if (!sessionId || !session) {
      outputBufferRef.current["web-fallback"] = "$ ";
      webLineRef.current = "";
      termRef.current?.clear();
      termRef.current?.write("$ ");
      termRef.current?.focus?.();
      return;
    }

    setStatusMessage("");
    if (isDesktop()) {
      const result = await ptyClear(session.id, session.conversationId);
      if (!result.cleared) {
        setStatusMessage("无法清空终端，因为该会话已不属于当前对话。");
        await refreshSessionsRef.current();
        return;
      }
      outputCursorRef.current[session.id] = result.outputCursor;
      // Keep the backend's reconnectable mirror consistent with the Electron
      // PTY. A disconnected backend does not invalidate the local clear.
      sendClientCommand(
        {
          type: "terminal.clear",
          session_id: session.id,
          conversation_id: session.conversationId,
          workspace_root: state.workingDirectory || undefined,
        },
        { silent: true },
      );
    } else {
      const result = await sendClientCommandAwaitResult(
        {
          type: "terminal.clear",
          session_id: session.id,
          conversation_id: session.conversationId,
          workspace_root: state.workingDirectory || undefined,
        },
        "terminal.clear",
      );
      if (!commandResultSucceeded(result)) {
        setStatusMessage(result.message || "清空终端失败。");
        return;
      }
    }

    outputBufferRef.current[session.id] = "";
    hydratedSnapshotRef.current.delete(session.id);
    state.upsertTerminalSnapshot({
      id: session.id,
      conversationId: session.conversationId,
      pid: session.pid,
      shell: session.shell,
      cwd: session.cwd,
      status: session.status,
      terminalMode: session.terminalMode,
      output: "",
      outputChars: 0,
      totalOutputChars: 0,
      truncated: false,
      capturedAt: Date.now(),
    });
    termRef.current?.clear();
    termRef.current?.focus?.();
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
          title="新建终端"
          aria-label="新建终端"
        >
          <Plus size={14} />
        </button>
        {terminalSessions.length > 0 && (
          <div
            role="tablist"
            aria-label="终端会话"
            className="flex flex-1 items-center gap-0.5 min-w-0 overflow-x-auto"
            style={{ scrollbarWidth: "thin" }}
          >
            {terminalSessions.map((session, index) => {
              const selected = session.id === activeTerminalSessionId;
              const label = terminalSessionLabel(session, index, terminalSessions.length);
              return (
                <div
                  key={session.id}
                  role="presentation"
                  onMouseDown={(event) => event.stopPropagation()}
                  className="group h-7 inline-flex items-center border-0 whitespace-nowrap"
                  style={{
                    borderRadius: "var(--radius-sm, 4px)",
                    background: selected ? "var(--surface-raised)" : "transparent",
                    boxShadow: selected ? "inset 0 -2px 0 var(--accent-primary)" : "none",
                  }}
                >
                  <button
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    title={`${label} - ${terminalStatusLabel(session.status)}`}
                    onClick={() => useAppStore.getState().setActiveTerminalSession(session.id)}
                    className="h-7 pl-2 pr-1 inline-flex items-center gap-1.5 border-0 bg-transparent cursor-pointer whitespace-nowrap"
                    style={{
                      color: selected ? "var(--text-primary)" : "var(--text-muted)",
                      fontSize: "var(--text-xs)",
                    }}
                  >
                    <span
                      aria-hidden="true"
                      className="w-1.5 h-1.5 rounded-full shrink-0"
                      style={{
                        background: session.status === "exited" ? "var(--text-muted)" : "var(--state-success)",
                      }}
                    />
                    <span>{label}</span>
                  </button>
                  <button
                    type="button"
                    title={`删除终端 ${label}`}
                    aria-label={`删除终端 ${label}`}
                    disabled={terminatingSessionIds.has(session.id)}
                    aria-busy={terminatingSessionIds.has(session.id)}
                    onClick={(event) => {
                      event.stopPropagation();
                      void killSession(session.id);
                    }}
                    className={`mr-0.5 w-5 h-5 inline-flex items-center justify-center border-0 bg-transparent cursor-pointer transition-opacity ${selected ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus-visible:opacity-100"}`}
                    style={{
                      color: "var(--text-muted)",
                      borderRadius: "var(--radius-sm, 4px)",
                    }}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              );
            })}
          </div>
        )}
        {liveUrl && (
          <button
            type="button"
            title={`在预览面板中打开 ${liveUrl}`}
            aria-label={`在预览面板中打开 ${liveUrl}`}
            onClick={() => {
              openWebInBrowser(liveUrl);
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
            <Globe size={14} />
            <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
              {liveUrl.replace(/^https?:\/\//, "")}
            </span>
            <ExternalLink size={14} />
          </button>
        )}
        <IconButton label="刷新终端列表" onClick={() => void refreshSessions()}>
          <RotateCcw size={14} />
        </IconButton>
        <IconButton label="复制选中的终端文本" onClick={() => void copyTerminalSelection()}>
          <Copy size={14} />
        </IconButton>
        <IconButton label="清空终端" onClick={() => void clearActiveTerminal()}>
          <Square size={14} />
        </IconButton>
        {activeSession?.status === "exited" && (
          <IconButton
            label="重新启动终端"
            onClick={() => void restartSession(activeSession.id)}
          >
            <RotateCcw size={14} />
          </IconButton>
        )}
      </div>
      <div
        ref={containerRef}
        className="flex-1 w-full min-h-0 p-0.5 overflow-hidden"
        style={{
          background: terminalTheme(resolvedTheme === "light").background,
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
