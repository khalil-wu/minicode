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
}

export type FsCompareWriteResult =
  | { ok: true; file: FsFileResponse }
  | { ok: false; conflict: true; message: string }
  | { ok: false; conflict: false; message: string };

export interface PtySession {
  sessionId: string;
  pid?: number;
  shell: string;
  cwd: string;
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

export interface BrowserNavigateOptions {
  allowPrivateNetwork?: boolean;
}

interface MiniCodeDesktop {
  platformInfo: { isDesktop: boolean; platform: string; arch: string };
  windowControls: {
    minimize(): Promise<void>;
    maximize(): Promise<void>;
    close(): Promise<void>;
  };
  notify(payload: { title: string; body: string }): Promise<void>;
  pickDirectory(): Promise<string | null>;
  trustWorkspace(path: string): Promise<string | null>;
  openExternal(target: string): Promise<void>;
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
    deletePath(path: string, recursive?: boolean): Promise<void>;
  };
  pty: {
    spawn(cwd?: string): Promise<{ sessionId?: string; session_id?: string; pid?: number; shell?: string; cwd?: string }>;
    write(sessionId: string, data: string): Promise<void>;
    resize(sessionId: string, cols: number, rows: number): Promise<void>;
    kill(sessionId: string): Promise<void>;
    list(): Promise<{ sessionId?: string; session_id?: string; pid?: number; shell?: string; cwd?: string }[]>;
    onData(cb: (data: { sessionId: string; data: string }) => void): void;
    onExit(cb: (data: { sessionId: string; exitCode: number }) => void): void;
  };
  env: {
    detect(): Promise<Partial<DesktopEnvInfo>>;
  };
  browser: {
    discover(endpoint?: string): Promise<BrowserDiscoveryResult>;
    captureScreenshot(endpoint: string | undefined, targetId: string): Promise<BrowserScreenshotResult>;
    navigate(endpoint: string | undefined, targetId: string, url: string, options?: BrowserNavigateOptions): Promise<BrowserActionResult>;
    click(endpoint: string | undefined, targetId: string, selector: string): Promise<BrowserActionResult>;
    type(endpoint: string | undefined, targetId: string, selector: string, text: string): Promise<BrowserActionResult>;
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

export const runtime = () => window.__MINICODE_RUNTIME__;

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
  return {
    sessionId,
    pid: typeof value.pid === "number" ? value.pid : undefined,
    shell: typeof value.shell === "string" ? value.shell : "shell",
    cwd: typeof value.cwd === "string" ? value.cwd : fallbackCwd,
  };
};

export const ptySpawn = async (cwd?: string): Promise<PtySession | null> => {
  try {
    return normalizePtySession(await desktop()?.pty.spawn(cwd), cwd ?? "");
  } catch {
    return null;
  }
};
export const ptyWrite = (sessionId: string, data: string) => desktop()?.pty.write(sessionId, data);
export const ptyResize = (sessionId: string, cols: number, rows: number) => desktop()?.pty.resize(sessionId, cols, rows);
export const ptyKill = (sessionId: string) => desktop()?.pty.kill(sessionId);
export const ptyList = async (): Promise<PtySession[]> => {
  try {
    const sessions = await desktop()?.pty.list();
    return (sessions ?? [])
      .map((session) => normalizePtySession(session))
      .filter((session): session is PtySession => session != null);
  } catch {
    return [];
  }
};

// --- Shell / OS ---

export const openExternal = (target: string) => desktop()?.openExternal(target);
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
  options?: BrowserNavigateOptions,
): Promise<BrowserActionResult | null> => {
  try {
    return (await desktop()?.browser.navigate(endpoint, targetId, url, options)) ?? null;
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
