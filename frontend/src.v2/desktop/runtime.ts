export interface FsEntry {
  name: string;
  path: string;
  isDirectory: boolean;
  sizeBytes?: number;
  modifiedAt?: string;
}

export interface FsSearchResult {
  path: string;
  name: string;
  score?: number;
  kind?: "file" | "folder";
}

export interface FsListTreeResult {
  workspaceRoot: string;
  requestedPath: string;
  entries: FsEntry[];
}

export interface FsFileResponse {
  path?: string;
  content: string;
  contentHash?: string;
  content_hash?: string;
  sizeBytes?: number;
  size_bytes?: number;
  modifiedAt?: string;
  modified_at?: string;
  readOnly?: boolean;
  read_only?: boolean;
  encoding?: "utf-8" | "utf-8-bom" | "utf-16le" | "utf-16be" | "gb18030" | string;
}

export type FsCompareWriteResult =
  | { ok: true; file: FsFileResponse }
  | { ok: false; conflict: true; message: string }
  | { ok: false; conflict: false; message: string };

export type FsDeletePathResult =
  | { needsConfirmation: true; path: string; entryCount: number }
  | { deleted: true; path: string; is_dir?: boolean; isDirectory?: boolean };

export interface PtySession {
  sessionId: string;
  conversationId: string;
  pid?: number;
  shell: string;
  cwd: string;
  output?: string;
  outputChars?: number;
  totalOutputChars?: number;
  outputStartCursor?: number;
  outputEndCursor?: number;
  truncated?: boolean;
  isAlive?: boolean;
  exitCode?: number;
  exitedAt?: number;
  terminalMode: "pty";
}

export interface DesktopEnvInfo {
  git: boolean;
  python: boolean;
  node: boolean;
  docker: boolean;
  ollama: boolean;
  home: string;
}

export interface BrowserTargetInfo {
  id: string;
  type: string;
  title: string;
  url: string;
  attached?: boolean;
  faviconUrl?: string;
  devtoolsFrontendUrl?: string;
  webSocketDebuggerUrl?: string;
}

export interface BrowserDiscoveryResult {
  status: "connected" | "error";
  endpoint: string;
  browser: string;
  protocolVersion: string;
  userAgent: string;
  webSocketDebuggerUrl: string;
  targets: BrowserTargetInfo[];
  error?: string;
}

export interface BrowserScreenshotResult {
  endpoint: string;
  targetId: string;
  title: string;
  url: string;
  mimeType: string;
  data: string;
  width?: number;
  height?: number;
  capturedAt: number;
}

export interface BrowserActionResult {
  action: "navigate" | "click" | "type";
  endpoint: string;
  targetId: string;
  title: string;
  url: string;
  screenshot: BrowserScreenshotResult;
}

export interface EmbeddedBrowserState {
  id: string;
  type: "page" | "loading" | "updated" | "error" | "new-tab-request";
  url: string;
  title: string;
  faviconUrl?: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  active?: boolean;
  error?: string;
  requestedUrl?: string;
}

export interface EmbeddedBrowserBounds {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface EmbeddedBrowserInspectionResult {
  ok: boolean;
  action?: string;
  value?: unknown;
  error?: string;
}

export interface EmbeddedBrowserSettings {
  downloadPolicy: "block" | "ask" | "allow";
  origin: string;
  permissions: string[];
}

interface MiniCodeDesktop {
  platformInfo: { isDesktop: boolean; platform: string; arch: string };
  windowControls: {
    minimize(): Promise<void>;
    maximize(): Promise<void>;
    close(): Promise<void>;
  };
  notify(payload: {
    title: string;
    body: string;
    target?: { kind: "conversation"; conversationId: string };
  }): Promise<void>;
  onDeepLink(callback: (payload: {
    id: string;
    target: { kind: "conversation"; conversationId: string } | { kind: "url"; url: string };
  }) => void): (() => void) | void;
  ackDeepLink(id: string): Promise<boolean>;
  updates: {
    check(): Promise<boolean>;
    download(): Promise<boolean>;
    install(): Promise<boolean>;
    getStatus(): Promise<{ status: string; version?: string; previousVersion?: string; failedVersion?: string; percent?: number; message?: string }>;
    onStatus(callback: (payload: { status: string; version?: string; previousVersion?: string; failedVersion?: string; percent?: number; message?: string }) => void): (() => void) | void;
  };
  pickDirectory(): Promise<string | null>;
  trustWorkspace(path: string): Promise<string | null>;
  openExternal(target: string): Promise<boolean>;
  openPath(target: string): Promise<boolean>;
  revealPath(target: string): Promise<void>;
  diagnostics: { export(): Promise<unknown> };
  fs: {
    listTree(path: string): Promise<FsEntry[] | {
      workspace_root?: string;
      workspaceRoot?: string;
      requested_path?: string;
      requestedPath?: string;
      entries?: unknown[];
    }>;
    searchFiles(rootPath: string, query: string, limit?: number): Promise<FsSearchResult[]>;
    searchFilesByKind?(rootPath: string, query: string, limit?: number, kind?: "file" | "folder" | "all"): Promise<FsSearchResult[]>;
    readFile(path: string): Promise<FsFileResponse>;
    writeFile(path: string, content: string): Promise<void>;
    compareWriteFile?(path: string, expectedHash: string, content: string): Promise<FsFileResponse>;
    createDirectory(path: string): Promise<void>;
    renamePath(oldPath: string, newPath: string): Promise<void>;
    deletePath(path: string, recursive?: boolean, confirm?: boolean): Promise<FsDeletePathResult>;
  };
  pty: {
    spawn(cwd: string | undefined, conversationId?: string): Promise<{ sessionId?: string; session_id?: string; conversationId?: string; conversation_id?: string; pid?: number; shell?: string; cwd?: string }>;
    spawnOwned?(cwd: string | undefined, conversationId: string): Promise<{ sessionId?: string; session_id?: string; conversationId?: string; conversation_id?: string; pid?: number; shell?: string; cwd?: string }>;
    write(sessionId: string, data: string, conversationId: string): Promise<void>;
    resize(sessionId: string, cols: number, rows: number, conversationId: string): Promise<void>;
    kill(sessionId: string, conversationId: string): Promise<boolean>;
    killConversation(conversationId: string): Promise<number>;
    list(conversationId?: string): Promise<Record<string, unknown>[]>;
    listOwned?(conversationId: string): Promise<Record<string, unknown>[]>;
    snapshot(sessionId: string, maxChars: number | undefined, conversationId: string): Promise<Record<string, unknown> | null>;
    ackExit(sessionId: string, conversationId: string): Promise<boolean>;
    onData(cb: (data: { sessionId: string; conversationId: string; data: string; startCursor?: number; endCursor?: number }) => void): (() => void) | void;
    onExit(cb: (data: { sessionId: string; conversationId: string; exitCode: number }) => void): (() => void) | void;
  };
  env: {
    detect(): Promise<Partial<DesktopEnvInfo>>;
  };
  browser: {
    discover(endpoint?: string): Promise<BrowserDiscoveryResult>;
    captureScreenshot(endpoint: string | undefined, targetId: string): Promise<BrowserScreenshotResult>;
    navigate(endpoint: string | undefined, targetId: string, url: string): Promise<BrowserActionResult>;
    click(endpoint: string | undefined, targetId: string, selector: string): Promise<BrowserActionResult>;
    type(endpoint: string | undefined, targetId: string, selector: string, text: string): Promise<BrowserActionResult>;
  };
  embeddedBrowser: {
    create(payload: { id: string; url: string }): Promise<EmbeddedBrowserState>;
    list(): Promise<EmbeddedBrowserState[]>;
    activate(id: string): Promise<boolean>;
    setBounds(payload: EmbeddedBrowserBounds): Promise<boolean>;
    navigate(payload: { id: string; url: string }): Promise<EmbeddedBrowserState>;
    runAction(payload: { id: string; action: "back" | "forward" | "reload" | "stop" | "focus" }): Promise<boolean>;
    inspect(payload: { id: string; kind: "console" | "network" | "element" | "region" }): Promise<EmbeddedBrowserInspectionResult>;
    getSettings(payload: { url: string }): Promise<EmbeddedBrowserSettings>;
    setSettings(payload: { downloadPolicy?: "block" | "ask" | "allow"; origin?: string; permission?: string; allowed?: boolean }): Promise<EmbeddedBrowserSettings>;
    clearSiteData(id: string): Promise<boolean>;
    close(id: string): Promise<boolean>;
    onEvent(callback: (payload: EmbeddedBrowserState) => void): (() => void) | void;
  };
}

declare global {
  interface Window {
    __MINICODE_RUNTIME__?: {
      apiBaseUrl?: string;
      wsBaseUrl?: string;
      runtimeToken?: string;
      desktop?: MiniCodeDesktop;
    };
  }
}

export const runtime = () =>
  typeof window !== "undefined" ? window.__MINICODE_RUNTIME__ : undefined;

export const desktop = (): MiniCodeDesktop | undefined => runtime()?.desktop;

export const isDesktop = (): boolean =>
  Boolean(desktop()?.platformInfo?.isDesktop);

// --- Directory picker ---

export const pickDirectory = async (): Promise<string | null> => {
  try {
    return (await desktop()?.pickDirectory()) ?? null;
  } catch {
    return null;
  }
};

export const trustWorkspace = async (path: string): Promise<string | null> => {
  try {
    return (await desktop()?.trustWorkspace(path)) ?? null;
  } catch {
    return null;
  }
};

// --- Filesystem ---

export const fsReadFile = async (path: string): Promise<string | null> => {
  try {
    const result = await desktop()?.fs.readFile(path);
    return result?.content ?? null;
  } catch {
    return null;
  }
};

export const fsReadFileInfo = async (path: string): Promise<FsFileResponse | null> => {
  const fs = desktop()?.fs;
  if (!fs) return null;
  return await fs.readFile(path);
};

export const fsWriteFile = async (path: string, content: string): Promise<boolean> => {
  try {
    await desktop()?.fs.writeFile(path, content);
    return true;
  } catch {
    return false;
  }
};

export const fsCompareWriteFile = async (
  path: string,
  expectedHash: string,
  content: string,
): Promise<FsCompareWriteResult> => {
  try {
    const fs = desktop()?.fs;
    if (!fs) {
      return { ok: false, conflict: false, message: "Save failed: desktop filesystem is unavailable." };
    }
    if (!fs?.compareWriteFile) {
      await fs.writeFile(path, content);
      return { ok: true, file: { path, content } };
    }
    return { ok: true, file: await fs.compareWriteFile(path, expectedHash, content) };
  } catch (error) {
    const message = error instanceof Error ? error.message : "Save failed.";
    const conflict =
      message.toLowerCase().includes("changed on disk") ||
      message.toLowerCase().includes("err_file_changed");
    return conflict
      ? { ok: false, conflict: true, message }
      : { ok: false, conflict: false, message };
  }
};

const normalizeFsEntry = (entry: unknown): FsEntry | null => {
  if (!entry || typeof entry !== "object") return null;
  const value = entry as Record<string, unknown>;
  const path = typeof value.path === "string" ? value.path : "";
  const name = typeof value.name === "string"
    ? value.name
    : path.split(/[/\\]/).filter(Boolean).pop() ?? path;
  if (!path && !name) return null;
  return {
    name,
    path,
    isDirectory: Boolean(value.isDirectory ?? value.is_dir),
    sizeBytes: typeof value.sizeBytes === "number"
      ? value.sizeBytes
      : typeof value.size_bytes === "number"
        ? value.size_bytes
        : typeof value.size === "number"
          ? value.size
          : undefined,
    modifiedAt: typeof value.modifiedAt === "string"
      ? value.modifiedAt
      : typeof value.modified_at === "string"
        ? value.modified_at
        : typeof value.modified === "number"
          ? new Date(value.modified).toISOString()
          : undefined,
  };
};

const normalizeFsListTreeResult = (path: string, payload: unknown): FsListTreeResult => {
  if (Array.isArray(payload)) {
    return {
      workspaceRoot: path,
      requestedPath: path,
      entries: payload.map(normalizeFsEntry).filter((entry): entry is FsEntry => entry != null),
    };
  }
  const value = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  const rawEntries = Array.isArray(value.entries) ? value.entries : [];
  return {
    workspaceRoot: typeof value.workspaceRoot === "string"
      ? value.workspaceRoot
      : typeof value.workspace_root === "string"
        ? value.workspace_root
        : path,
    requestedPath: typeof value.requestedPath === "string"
      ? value.requestedPath
      : typeof value.requested_path === "string"
        ? value.requested_path
        : path,
    entries: rawEntries.map(normalizeFsEntry).filter((entry): entry is FsEntry => entry != null),
  };
};

export const fsListTreeResult = async (path: string): Promise<FsListTreeResult> => {
  try {
    return normalizeFsListTreeResult(path, await desktop()?.fs.listTree(path));
  } catch (err) {
    console.warn("[fsListTree] failed for", path, err);
    return { workspaceRoot: path, requestedPath: path, entries: [] };
  }
};

export const fsListTree = async (path: string): Promise<FsEntry[]> =>
  (await fsListTreeResult(path)).entries;

export const fsSearchFiles = async (
  rootPath: string,
  query: string,
  limit = 20,
  kind: "file" | "folder" | "all" = "file",
): Promise<FsSearchResult[]> => {
  try {
    const fs = desktop()?.fs;
    const result = kind !== "all" && fs?.searchFilesByKind
      ? await fs.searchFilesByKind(rootPath, query, limit, kind)
      : await fs?.searchFiles(rootPath, query, limit);
    return normalizeFsSearchResult(result);
  } catch {
    return [];
  }
};

const normalizeFsSearchResult = (payload: unknown): FsSearchResult[] => {
  const raw = Array.isArray(payload)
    ? payload
    : payload && typeof payload === "object" && Array.isArray((payload as Record<string, unknown>).results)
      ? (payload as Record<string, unknown>).results as unknown[]
      : [];
  return raw
    .map((item): FsSearchResult | null => {
      if (!item || typeof item !== "object") return null;
      const value = item as Record<string, unknown>;
      const path = typeof value.path === "string" ? value.path : "";
      if (!path) return null;
      const name = typeof value.name === "string"
        ? value.name
        : path.split(/[/\\]/).filter(Boolean).pop() ?? path;
      const result: FsSearchResult = {
        path,
        name,
        kind: value.kind === "folder" || value.isDirectory === true || value.is_dir === true ? "folder" as const : "file" as const,
      };
      if (typeof value.score === "number") result.score = value.score;
      return result;
    })
    .filter((item): item is FsSearchResult => item != null);
};

// --- PTY ---

const normalizePtySession = (session: unknown, fallbackCwd = ""): PtySession | null => {
  if (!session || typeof session !== "object") return null;
  const value = session as Record<string, unknown>;
  const sessionId = typeof value.sessionId === "string"
    ? value.sessionId
    : typeof value.session_id === "string"
      ? value.session_id
      : "";
  if (!sessionId) return null;
  const output = typeof value.output === "string" ? value.output : undefined;
  const outputChars = typeof value.outputChars === "number"
    ? value.outputChars
    : typeof value.output_chars === "number"
      ? value.output_chars
      : undefined;
  const totalOutputChars = typeof value.totalOutputChars === "number"
    ? value.totalOutputChars
    : typeof value.total_output_chars === "number"
      ? value.total_output_chars
      : undefined;
  const outputStartCursor = typeof value.outputStartCursor === "number"
    ? value.outputStartCursor
    : typeof value.output_start_cursor === "number"
      ? value.output_start_cursor
      : undefined;
  const outputEndCursor = typeof value.outputEndCursor === "number"
    ? value.outputEndCursor
    : typeof value.output_end_cursor === "number"
      ? value.output_end_cursor
      : undefined;
  const isAlive = typeof value.isAlive === "boolean"
    ? value.isAlive
    : typeof value.is_alive === "boolean"
      ? value.is_alive
      : undefined;
  const exitCode = typeof value.exitCode === "number"
    ? value.exitCode
    : typeof value.exit_code === "number"
      ? value.exit_code
      : undefined;
  const exitedAt = typeof value.exitedAt === "number"
    ? value.exitedAt
    : typeof value.exited_at === "number"
      ? value.exited_at
      : undefined;
  const conversationId = typeof value.conversationId === "string"
    ? value.conversationId.trim()
    : typeof value.conversation_id === "string"
      ? value.conversation_id.trim()
      : "";
  if (!conversationId) return null;
  return {
    sessionId,
    conversationId,
    pid: typeof value.pid === "number" ? value.pid : undefined,
    shell: typeof value.shell === "string" ? value.shell : "shell",
    cwd: typeof value.cwd === "string" ? value.cwd : fallbackCwd,
    ...(output !== undefined ? { output } : {}),
    ...(outputChars !== undefined ? { outputChars } : {}),
    ...(totalOutputChars !== undefined ? { totalOutputChars } : {}),
    ...(outputStartCursor !== undefined ? { outputStartCursor } : {}),
    ...(outputEndCursor !== undefined ? { outputEndCursor } : {}),
    ...(typeof value.truncated === "boolean" ? { truncated: value.truncated } : {}),
    ...(isAlive !== undefined ? { isAlive } : {}),
    ...(exitCode !== undefined ? { exitCode } : {}),
    ...(exitedAt !== undefined ? { exitedAt } : {}),
    terminalMode: "pty",
  };
};

export const ptySpawn = async (cwd: string | undefined, conversationId: string): Promise<PtySession | null> => {
  const owner = conversationId.trim();
  if (!owner) return null;
  try {
    const pty = desktop()?.pty;
    if (!pty) return null;
    const raw = pty.spawnOwned
      ? await pty.spawnOwned(cwd, owner)
      : await pty.spawn(cwd, owner);
    const session = normalizePtySession(raw, cwd ?? "");
    return session?.conversationId === owner ? session : null;
  } catch {
    return null;
  }
};
export const ptyWrite = (sessionId: string, data: string, conversationId: string) => desktop()?.pty.write(sessionId, data, conversationId);
export const ptyResize = (sessionId: string, cols: number, rows: number, conversationId: string) => desktop()?.pty.resize(sessionId, cols, rows, conversationId);
export const ptyKill = (sessionId: string, conversationId: string) => desktop()?.pty.kill(sessionId, conversationId);
export const ptyKillConversation = (conversationId: string) => desktop()?.pty.killConversation(conversationId);
export const ptyAckExit = (sessionId: string, conversationId: string) => desktop()?.pty.ackExit(sessionId, conversationId);
export const ptyList = async (conversationId: string): Promise<PtySession[]> => {
  const owner = conversationId.trim();
  if (!owner) return [];
  try {
    const pty = desktop()?.pty;
    if (!pty) return [];
    const sessions = pty.listOwned ? await pty.listOwned(owner) : await pty.list(owner);
    return (sessions ?? [])
      .map((session) => normalizePtySession(session))
      .filter((session): session is PtySession => session?.conversationId === owner);
  } catch {
    return [];
  }
};
export const ptySnapshot = async (sessionId: string, conversationId: string, maxChars = 80_000): Promise<PtySession | null> => {
  const owner = conversationId.trim();
  if (!owner) return null;
  try {
    const session = normalizePtySession(await desktop()?.pty.snapshot(sessionId, maxChars, owner));
    return session?.conversationId === owner ? session : null;
  } catch {
    return null;
  }
};

// --- Shell / OS ---

export const openExternal = (target: string) => desktop()?.openExternal(target);
export const openPath = (target: string) => desktop()?.openPath(target);
export const revealPath = (target: string) => desktop()?.revealPath(target);
export const exportDiagnostics = () => desktop()?.diagnostics.export();
export const browserDiscover = async (endpoint?: string): Promise<BrowserDiscoveryResult | null> => {
  try {
    return (await desktop()?.browser.discover(endpoint)) ?? null;
  } catch {
    return null;
  }
};
export const browserCaptureScreenshot = async (
  endpoint: string | undefined,
  targetId: string,
): Promise<BrowserScreenshotResult | null> => {
  try {
    return (await desktop()?.browser.captureScreenshot(endpoint, targetId)) ?? null;
  } catch {
    return null;
  }
};

export const browserNavigate = async (
  endpoint: string | undefined,
  targetId: string,
  url: string,
): Promise<BrowserActionResult | null> => {
  try {
    return (await desktop()?.browser.navigate(endpoint, targetId, url)) ?? null;
  } catch {
    return null;
  }
};

export const browserClick = async (
  endpoint: string | undefined,
  targetId: string,
  selector: string,
): Promise<BrowserActionResult | null> => {
  try {
    return (await desktop()?.browser.click(endpoint, targetId, selector)) ?? null;
  } catch {
    return null;
  }
};

export const browserType = async (
  endpoint: string | undefined,
  targetId: string,
  selector: string,
  text: string,
): Promise<BrowserActionResult | null> => {
  try {
    return (await desktop()?.browser.type(endpoint, targetId, selector, text)) ?? null;
  } catch {
    return null;
  }
};

export const embeddedBrowserCreate = (id: string, url: string) =>
  desktop()?.embeddedBrowser.create({ id, url });
export const embeddedBrowserList = () =>
  desktop()?.embeddedBrowser.list();
export const embeddedBrowserActivate = (id: string) =>
  desktop()?.embeddedBrowser.activate(id);
export const embeddedBrowserSetBounds = (bounds: EmbeddedBrowserBounds) =>
  desktop()?.embeddedBrowser.setBounds(bounds);
export const embeddedBrowserNavigate = (id: string, url: string) =>
  desktop()?.embeddedBrowser.navigate({ id, url });
export const embeddedBrowserRunAction = (
  id: string,
  action: "back" | "forward" | "reload" | "stop" | "focus",
) => desktop()?.embeddedBrowser.runAction({ id, action });
export const embeddedBrowserInspect = (id: string, kind: "console" | "network" | "element" | "region") =>
  desktop()?.embeddedBrowser.inspect({ id, kind });
export const embeddedBrowserGetSettings = (url: string) => desktop()?.embeddedBrowser.getSettings({ url });
export const embeddedBrowserSetSettings = (payload: {
  downloadPolicy?: "block" | "ask" | "allow";
  origin?: string;
  permission?: string;
  allowed?: boolean;
}) => desktop()?.embeddedBrowser.setSettings(payload);
export const embeddedBrowserClearSiteData = (id: string) => desktop()?.embeddedBrowser.clearSiteData(id);
export const embeddedBrowserClose = (id: string) =>
  desktop()?.embeddedBrowser.close(id);
export const onEmbeddedBrowserEvent = (callback: (payload: EmbeddedBrowserState) => void) =>
  desktop()?.embeddedBrowser.onEvent(callback);

// --- Environment ---

export const envDetect = async (): Promise<DesktopEnvInfo | null> => {
  try {
    const info = await desktop()?.env.detect();
    if (!info) return null;
    return {
      git: Boolean(info.git),
      python: Boolean(info.python),
      node: Boolean(info.node),
      docker: Boolean(info.docker),
      ollama: Boolean(info.ollama),
      home: typeof info.home === "string" ? info.home : "",
    };
  } catch {
    return null;
  }
};
