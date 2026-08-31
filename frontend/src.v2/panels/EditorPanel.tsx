import { useEffect, useMemo, useRef, useState } from "react";
import { lazy, Suspense } from "react";
import { Circle, Edit3, Eye, FileCode2, FileWarning, GitCompare, Image, LockKeyhole, RefreshCw, X } from "lucide-react";
import { fileGlyphColor, fileIcon } from "../shell/fileTreeHelpers";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import { useAppStore } from "../stores";
import { editorPathComparisonKey, editorPathsEqual } from "../stores/shared-helpers";
import { workspaceRawResourceUrlWithToken } from "../protocol/api";
import {
  compareWriteWorkspaceFile,
  readWorkspaceFile,
  searchWorkspaceFiles,
} from "../protocol/workspace";
import { fsReadFileInfo, fsSearchFiles, isDesktop, revealPath } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import {
  isWindowsLikeWorkspacePath,
  normalizeWorkspacePath,
  normalizeWorkspaceRoot,
  workspacePathWithin,
  workspacePathsEqual,
  workspaceRootsEqual,
} from "../lib/workspace-path";
import { isImagePath, isPdfPath, isPreviewableMediaPath } from "../lib/media-types";
import { formatBytes } from "../lib/format-bytes";

const configureMonacoWorkers = () => {
  const scope = globalThis as typeof globalThis & {
    MonacoEnvironment?: {
      getWorker?: (_workerId: string, label: string) => Worker;
    };
  };
  if (scope.MonacoEnvironment?.getWorker) return;
  scope.MonacoEnvironment = {
    ...scope.MonacoEnvironment,
    getWorker: () => new EditorWorker(),
  };
};

const LazyMonacoEditor = lazy(async () => {
  configureMonacoWorkers();
  const [reactMonaco, monaco] = await Promise.all([
    import("@monaco-editor/react"),
    import("monaco-editor/editor/editor.api.js"),
    import("monaco-editor/languages/definitions/typescript/register.js"),
    import("monaco-editor/languages/definitions/javascript/register.js"),
    import("monaco-editor/languages/definitions/css/register.js"),
    import("monaco-editor/languages/definitions/html/register.js"),
    import("monaco-editor/languages/definitions/markdown/register.js"),
    import("monaco-editor/languages/definitions/python/register.js"),
  ]);
  reactMonaco.loader?.config?.({ monaco });
  return { default: reactMonaco.default };
});

type MonacoEditorInstance = {
  getSelection: () => unknown;
  getModel?: () => { getValueInRange: (range: unknown) => string } | null;
  addAction?: (descriptor: {
    id: string;
    label: string;
    contextMenuGroupId?: string;
    contextMenuOrder?: number;
    run: (editor: MonacoEditorInstance) => void;
  }) => unknown;
  executeEdits: (source: string, edits: Array<{ range: unknown; text: string; forceMoveMarkers?: boolean }>) => void;
  focus: () => void;
  revealLineInCenter?: (lineNumber: number) => void;
  revealPositionInCenter?: (position: { lineNumber: number; column: number }) => void;
  setPosition?: (position: { lineNumber: number; column: number }) => void;
  onDidChangeCursorPosition: (handler: (event: { position: { lineNumber: number; column: number } }) => void) => unknown;
};

type EditorInsertEvent = CustomEvent<{ text: string; handled?: boolean }>;
type EditorTarget = { path: string; line?: number; column?: number };

type PlainTextEditorProps = {
  value: string;
  onChange: (value: string) => void;
  onCursorChange: (cursor: { line: number; column: number }) => void;
  readOnly?: boolean;
};

const guessLanguage = (path: string): string => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (["ts", "tsx"].includes(ext)) return "typescript";
  if (["js", "jsx"].includes(ext)) return "javascript";
  if (ext === "py") return "python";
  if (ext === "json") return "json";
  if (ext === "md") return "markdown";
  if (ext === "css") return "css";
  if (ext === "html") return "html";
  if (["yml", "yaml"].includes(ext)) return "yaml";
  if (ext === "toml") return "toml";
  return "plaintext";
};

const basename = (path: string) => path.split(/[/\\]/).filter(Boolean).pop() ?? path;
const dirname = (path: string) => path.replace(/\\/g, "/").split("/").filter(Boolean).slice(0, -1).join("/");

const workspaceRelativePath = (path: string, workingDirectory: string): string => {
  const normalized = normalizeWorkspacePath(path);
  const root = normalizeWorkspacePath(workingDirectory);
  if (root && workspacePathWithin(normalized, root)) {
    return normalized.slice(root.length).replace(/^\/+/, "");
  }
  return normalized.replace(/^\.\/+/, "");
};

const resolveUnqualifiedEditorPath = async (path: string, workingDirectory: string): Promise<string> => {
  const relative = workspaceRelativePath(path, workingDirectory);
  if (!relative || relative.includes("/") || !workingDirectory.trim()) return relative || path;

  const query = basename(relative);
  const results = isDesktop()
    ? await fsSearchFiles(workingDirectory, query, 50, "file")
    : await searchWorkspaceFiles(workingDirectory, query, 50, "file");
  const compareName = (value: string): string =>
    isWindowsLikeWorkspacePath(workingDirectory) ? value.toLowerCase() : value;
  const exact = results.filter((result) => compareName(result.name) === compareName(query));
  if (exact.length !== 1) return relative;
  return workspaceRelativePath(exact[0].path, workingDirectory);
};

const cursorFromOffset = (value: string, offset: number): { line: number; column: number } => {
  const safeOffset = Math.max(0, Math.min(offset, value.length));
  const before = value.slice(0, safeOffset);
  const lines = before.split("\n");
  return {
    line: lines.length,
    column: (lines[lines.length - 1]?.length ?? 0) + 1,
  };
};

const UNSUPPORTED_EDITOR_EXTENSIONS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "avif",
  "tif",
  "tiff",
  "heic",
  "heif",
  "ico",
  "bmp",
  "pdf",
  "doc",
  "docx",
  "xls",
  "xlsx",
  "ppt",
  "pptx",
  "odt",
  "ods",
  "odp",
  "zip",
  "gz",
  "tar",
  "7z",
  "exe",
  "dll",
  "bin",
  "ttf",
  "otf",
  "woff",
  "woff2",
]);

const isMarkdownPath = (path: string): boolean => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return ext === "md" || ext === "mdx";
};

const isEditablePath = (path: string): boolean => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return !UNSUPPORTED_EDITOR_EXTENSIONS.has(ext);
};

const toWorkspaceDisplayPath = (path: string, workingDirectory = ""): string => {
  const decodedSeparators = path.trim().replace(/%5[cC]/g, "/").replace(/%2[fF]/g, "/");
  const normalized = normalizeWorkspacePath(decodedSeparators);
  const root = normalizeWorkspacePath(workingDirectory);
  if (!normalized || !root) return normalized;
  if (workspacePathsEqual(normalized, root)) return ".";
  if (workspacePathWithin(normalized, root)) {
    return normalized.slice(root.length).replace(/^\/+/, "") || ".";
  }
  return normalized;
};

const rawFileUrl = (path: string, workingDirectory = ""): string => {
  const normalized = toWorkspaceDisplayPath(path, workingDirectory);
  if (/^(https?:|data:|blob:|file:|mailto:|tel:|#)/i.test(normalized)) return normalized;
  return workspaceRawResourceUrlWithToken(normalized, workingDirectory);
};

const isAbsoluteLocalPath = (path: string): boolean =>
  /^[a-zA-Z]:(?:[\\/]|%5[cC]|%2[fF])/.test(path) || path.startsWith("/") || path.startsWith("\\");

const markdownUrlTransform = (url: string): string => (
  isAbsoluteLocalPath(url) ? url : defaultUrlTransform(url)
);

const normalizeJoinedPath = (path: string): string => {
  const normalized = path.replace(/\\/g, "/").replace(/\/+/g, "/");
  const driveMatch = normalized.match(/^([a-zA-Z]:)(?:\/|$)/);
  const prefix = driveMatch ? `${driveMatch[1]}/` : normalized.startsWith("/") ? "/" : "";
  const body = driveMatch ? normalized.slice(driveMatch[0].length) : normalized.replace(/^\/+/, "");
  const parts: string[] = [];
  for (const part of body.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (parts.length > 0 && parts[parts.length - 1] !== "..") parts.pop();
      else if (!prefix) parts.push(part);
      continue;
    }
    parts.push(part);
  }
  return `${prefix}${parts.join("/")}`.replace(/\/+$/, "") || ".";
};

const resolveWorkspaceAssetPath = (src: string, ownerPath: string, workingDirectory: string): string => {
  const trimmed = src.trim();
  if (!trimmed || /^(https?:|data:|blob:|file:|mailto:|tel:|#)/i.test(trimmed)) return trimmed;
  if (isAbsoluteLocalPath(trimmed)) return toWorkspaceDisplayPath(normalizeJoinedPath(trimmed), workingDirectory);
  const ownerDir = dirname(ownerPath);
  const base = ownerDir || workingDirectory || "";
  return toWorkspaceDisplayPath(normalizeJoinedPath(base ? `${base}/${trimmed}` : trimmed), workingDirectory);
};

const resolveDesktopFsPath = (path: string, workingDirectory: string): string => {
  const trimmed = path.trim();
  if (!trimmed || isAbsoluteLocalPath(trimmed) || !workingDirectory.trim()) return trimmed;
  return normalizeJoinedPath(`${workingDirectory}/${trimmed}`);
};

const pathsMatch = (a: string, b: string): boolean => {
  const left = a.replace(/\\/g, "/").replace(/^\/+/, "");
  const right = b.replace(/\\/g, "/").replace(/^\/+/, "");
  const caseInsensitive = isWindowsLikeWorkspacePath(a) || isWindowsLikeWorkspacePath(b);
  const leftKey = caseInsensitive ? left.toLowerCase() : left;
  const rightKey = caseInsensitive ? right.toLowerCase() : right;
  return leftKey === rightKey
    || leftKey.endsWith(`/${rightKey}`)
    || rightKey.endsWith(`/${leftKey}`);
};

interface FileSnapshot {
  content: string;
  contentHash?: string;
  sizeBytes?: number;
  readOnly?: boolean;
}

const MAX_EDITOR_BYTES = 2 * 1024 * 1024;
const MAX_EDITOR_CHARS = 1_000_000;
const MAX_EDITOR_LINES = 20_000;
const MAX_MARKDOWN_PREVIEW_IMAGES = 80;
const EDITOR_FRAME_REFERRER_POLICY = "no-referrer";

const countLines = (content: string): number =>
  content ? content.split(/\r\n|\r|\n/).length : 0;

const countMarkdownPreviewImages = (content: string): number => {
  const markdownImages = content.match(/!\[[^\]]*]\([^\)\r\n]*\)/g)?.length ?? 0;
  const htmlImages = content.match(/<img\b/gi)?.length ?? 0;
  return markdownImages + htmlImages;
};

const largeFileReason = (snapshot: FileSnapshot): string | null => {
  const bytes = snapshot.sizeBytes;
  if (bytes != null && bytes > MAX_EDITOR_BYTES) {
    return `该文件大小为 ${formatBytes(bytes)}，超过编辑器 ${formatBytes(MAX_EDITOR_BYTES)} 的限制。`;
  }
  if (snapshot.content.length > MAX_EDITOR_CHARS) {
    return `该文件包含 ${snapshot.content.length.toLocaleString()} 个字符，超过编辑器限制。`;
  }
  const lines = countLines(snapshot.content);
  if (lines > MAX_EDITOR_LINES) {
    return `该文件包含 ${lines.toLocaleString()} 行，超过编辑器限制。`;
  }
  return null;
};

const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error || "无法读取文件。");

const isLargeFileError = (message: string): boolean =>
  /too large|max supported size|above the .*limit|413/i.test(message);

const createMarkdownPreviewComponents = (
  ownerPath: string,
  workingDirectory: string,
  scopeId: string,
) => {
  const slugCounts = new Map<string, number>();
  const headingId = (children: React.ReactNode): string => {
    const base = markdownHeadingSlug(reactNodeText(children));
    const count = (slugCounts.get(base) ?? 0) + 1;
    slugCounts.set(base, count);
    return `${scopeId}-${base}${count > 1 ? `-${count}` : ""}`;
  };
  const heading = (level: 1 | 2 | 3) => (props: React.HTMLAttributes<HTMLHeadingElement>) => {
    const id = headingId(props.children);
    const Tag: "h1" | "h2" | "h3" = level === 1 ? "h1" : level === 2 ? "h2" : "h3";
    return <Tag {...props} id={id} style={{ scrollMarginTop: 16, ...props.style }} tabIndex={-1} />;
  };
  return {
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
    const href = typeof props.href === "string" ? props.href : "";
    if (href.startsWith("#")) {
      const target = `${scopeId}-${markdownHeadingSlug(decodeURIComponent(href.slice(1)))}`;
      return (
        <a
          {...props}
          href={`#${target}`}
          style={{ color: "var(--accent-primary)" }}
          onClick={(event) => {
            event.preventDefault();
            const element = document.getElementById(target);
            element?.scrollIntoView({ behavior: "smooth", block: "start" });
            element?.focus({ preventScroll: true });
          }}
        />
      );
    }
    return (
      <a
        {...props}
        href={href ? rawFileUrl(resolveWorkspaceAssetPath(href, ownerPath, workingDirectory)) : props.href}
        target="_blank"
        rel="noreferrer"
        style={{ color: "var(--accent-primary)" }}
      />
    );
  },
  img: (props: React.ImgHTMLAttributes<HTMLImageElement>) => {
    const src = typeof props.src === "string"
      ? rawFileUrl(resolveWorkspaceAssetPath(props.src, ownerPath, workingDirectory))
      : props.src;
    return (
      <img
        {...props}
        src={src}
        loading="lazy"
        decoding="async"
        style={{
          maxWidth: "100%",
          maxHeight: 520,
          objectFit: "contain",
          display: "block",
          margin: "10px 0",
          borderRadius: "var(--radius-sm, 4px)",
          border: "1px solid var(--border-subtle)",
          background: "var(--surface-soft)",
          ...props.style,
        }}
      />
    );
  },
  pre: (props: React.HTMLAttributes<HTMLPreElement>) => (
    <pre
      {...props}
      style={{
        overflowX: "auto",
        margin: "10px 0",
        padding: "12px 14px",
        borderRadius: "var(--radius-sm, 6px)",
        border: "1px solid var(--border-subtle)",
        background: "var(--surface-soft)",
        ...props.style,
      }}
    />
  ),
  code: (props: React.HTMLAttributes<HTMLElement>) => (
    <code
      {...props}
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: "0.92em",
        ...props.style,
      }}
    />
  ),
  table: (props: React.TableHTMLAttributes<HTMLTableElement>) => (
    <div style={{ overflowX: "auto", margin: "10px 0" }}>
      <table {...props} style={{ borderCollapse: "collapse", width: "100%", ...props.style }} />
    </div>
  ),
  th: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
    <th
      {...props}
      style={{
        border: "1px solid var(--border-subtle)",
        padding: "6px 10px",
        background: "var(--surface-soft)",
        textAlign: "left",
        ...props.style,
      }}
    />
  ),
  td: (props: React.TdHTMLAttributes<HTMLTableCellElement>) => (
    <td {...props} style={{ border: "1px solid var(--border-subtle)", padding: "6px 10px", ...props.style }} />
  ),
  h1: heading(1),
  h2: heading(2),
  h3: heading(3),
};
};

const reactNodeText = (node: React.ReactNode): string => {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(reactNodeText).join("");
  if (node && typeof node === "object" && "props" in node) {
    return reactNodeText((node as React.ReactElement<{ children?: React.ReactNode }>).props.children);
  }
  return "";
};

const markdownHeadingSlug = (value: string): string => {
  const slug = value
    .trim()
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/[\s_]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "section";
};

const readFileSnapshot = async (path: string, workingDirectory: string): Promise<FileSnapshot | null> => {
  if (!isEditablePath(path)) return null;
  if (isDesktop()) {
    const desktopFile = await fsReadFileInfo(resolveDesktopFsPath(path, workingDirectory));
    if (desktopFile != null) {
      return {
        content: desktopFile.content,
        contentHash: desktopFile.contentHash ?? desktopFile.content_hash,
        sizeBytes: desktopFile.sizeBytes ?? desktopFile.size_bytes,
        readOnly: desktopFile.readOnly ?? desktopFile.read_only ?? false,
      };
    }
  }
  const response = await readWorkspaceFile(path, workingDirectory);
  if (!response) return null;
  return {
    content: response.content,
    contentHash: response.content_hash,
    sizeBytes: response.size_bytes ?? response.size,
  };
};

export const EditorPanel = ({ chrome = "full" }: { chrome?: "full" | "minimal" } = {}) => {
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const codeTextScale = useAppStore((s) => s.codeTextScale);
  const reducedMotion = useAppStore((s) => s.reducedMotion);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const editorOpenRequests = useAppStore((s) => s.editorOpenRequests);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const fileChanges = useAppStore((s) => s.fileChanges);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const setDiffReviewState = useAppStore((s) => s.setDiffReviewState);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const consumeEditorOpenRequest = useAppStore((s) => s.consumeEditorOpenRequest);
  const panelSlots = useAppStore((s) => s.panelSlots);
  const focusPanel = useAppStore((s) => s.focusPanel);
  const removePanel = useAppStore((s) => s.removePanel);
  const togglePanelMaximized = useAppStore((s) => s.togglePanelMaximized);

  const tabs = useAppStore((s) => s.editorTabs);
  const activeTabPath = useAppStore((s) => s.activeTabPath);
  const openEditorTab = useAppStore((s) => s.openEditorTab);
  const closeEditorTab = useAppStore((s) => s.closeEditorTab);
  const setActiveTab = useAppStore((s) => s.setActiveTab);
  const updateTabContent = useAppStore((s) => s.updateTabContent);
  const markTabLoaded = useAppStore((s) => s.markTabLoaded);
  const markTabSaved = useAppStore((s) => s.markTabSaved);
  const markTabExternalChanged = useAppStore((s) => s.markTabExternalChanged);
  const reloadTab = useAppStore((s) => s.reloadTab);

  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saved" | "error">("idle");
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; path: string } | null>(null);
  const [mdPreview, setMdPreview] = useState(false);
  const [monacoUnavailable, setMonacoUnavailable] = useState(false);
  const editorRef = useRef<MonacoEditorInstance | null>(null);
  const monacoMountedRef = useRef(false);
  const pendingRevealRef = useRef<EditorTarget | null>(null);
  const loadEpochRef = useRef(new Map<string, number>());

  const loadEpochKey = (path: string, directory: string): string =>
    `${normalizeWorkspaceRoot(directory)}\0${editorPathComparisonKey(path, directory)}`;

  const activeTab = tabs.find((tab) => editorPathsEqual(tab.path, activeTabPath, workingDirectory)) ?? null;
  const markdownScopeId = useMemo(
    () => `editor-markdown-${Math.abs(editorPathComparisonKey(activeTab?.path ?? "markdown", workingDirectory).split("").reduce((hash, char) => ((hash * 31) + char.charCodeAt(0)) | 0, 0))}`,
    [activeTab?.path, workingDirectory],
  );
  const editorSlot = panelSlots.find((slot) => slot.kind === "editor");
  const editorSlotId = editorSlot?.id ?? "editor";
  const chatSlot = panelSlots.find((slot) => slot.kind === "chat");
  const dirty = activeTab ? !activeTab.readOnly && activeTab.content !== activeTab.original : false;
  const language = useMemo(() => guessLanguage(activeTabPath ?? ""), [activeTabPath]);
  const monacoTheme = resolvedTheme === "light" ? "light" : "vs-dark";
  const canRenderMarkdown = Boolean(activeTab && isMarkdownPath(activeTab.path) && !activeTab.loading && !activeTab.error && !activeTab.largeFile);
  const markdownImageCount = useMemo(
    () => canRenderMarkdown && activeTab ? countMarkdownPreviewImages(activeTab.content) : 0,
    [activeTab, canRenderMarkdown],
  );
  const markdownPreviewComponents = useMemo(
    () => createMarkdownPreviewComponents(activeTab?.path ?? "", workingDirectory, markdownScopeId),
    [activeTab?.path, activeTab?.content, workingDirectory, markdownScopeId],
  );
  const markdownPreviewTooImageHeavy = markdownImageCount > MAX_MARKDOWN_PREVIEW_IMAGES;
  const showEditorTabs = chrome === "full";
  const activeGitChange = useMemo(() => {
    if (!activeTabPath) return null;
    const files = [
      ...gitChanges.workingTree,
      ...gitChanges.staged,
    ];
    return files.find((file) => pathsMatch(file.path, activeTabPath) && file.patch) ?? null;
  }, [activeTabPath, gitChanges.workingTree, gitChanges.staged]);

  useEffect(() => {
    setMdPreview(false);
  }, [activeTabPath]);

  useEffect(() => {
    if (
      monacoUnavailable ||
      editorRef.current ||
      !activeTab ||
      activeTab.loading ||
      activeTab.error ||
      activeTab.largeFile ||
      isImagePath(activeTab.path) ||
      isPdfPath(activeTab.path) ||
      mdPreview
    ) {
      return;
    }
    monacoMountedRef.current = false;
    const path = activeTab.path;
    const id = window.setTimeout(() => {
      const currentState = useAppStore.getState();
      if (!monacoMountedRef.current && editorPathsEqual(currentState.activeTabPath, path, currentState.workingDirectory)) {
        setMonacoUnavailable(true);
        console.warn("[EditorPanel] Monaco did not mount; falling back to the plain text editor.");
      }
    }, 2200);
    return () => window.clearTimeout(id);
  }, [activeTab?.path, activeTab?.loading, activeTab?.error, activeTab?.largeFile, mdPreview, monacoUnavailable]);

  // Consume open requests from other panels
  useEffect(() => {
    for (const request of editorOpenRequests) {
      consumeEditorOpenRequest(request.id);
      void resolveUnqualifiedEditorPath(request.path, workingDirectory).catch(() => (
        workspaceRelativePath(request.path, workingDirectory) || request.path
      )).then((resolvedPath) => {
        if (!workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory)) return;
        openEditorTab(resolvedPath);
        loadFileIfNeeded(resolvedPath);
        handleSetActive(resolvedPath, {
          path: resolvedPath,
          line: request.line,
          column: request.column,
        });
      });
    }
  }, [editorOpenRequests, consumeEditorOpenRequest, openEditorTab, workingDirectory]);

  // Sync activeEditorPath from workspace slice
  useEffect(() => {
    if (activeEditorPath && !editorPathsEqual(activeEditorPath, activeTabPath, workingDirectory)) {
      const exists = tabs.some((tab) => editorPathsEqual(tab.path, activeEditorPath, workingDirectory));
      if (exists) setActiveTab(activeEditorPath);
    }
  }, [activeEditorPath, activeTabPath, tabs, setActiveTab, workingDirectory]);

  // Load persisted tabs on mount
  useEffect(() => {
    for (const tab of tabs) {
      if (tab.loading) {
        void loadFileContent(tab.path);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadFileContent = async (path: string) => {
    const directory = workingDirectory;
    const epochKey = loadEpochKey(path, directory);
    const epoch = (loadEpochRef.current.get(epochKey) ?? 0) + 1;
    loadEpochRef.current.set(epochKey, epoch);
    const commit = (callback: () => void) => {
      if (loadEpochRef.current.get(epochKey) !== epoch) return;
      if (!workspaceRootsEqual(directory, useAppStore.getState().workingDirectory)) return;
      const currentState = useAppStore.getState();
      if (!currentState.editorTabs.some((tab) => editorPathsEqual(tab.path, path, currentState.workingDirectory))) return;
      callback();
    };
    if (isImagePath(path) || isPdfPath(path)) {
      commit(() => markTabLoaded(path, "", null));
      return;
    }
    if (!isEditablePath(path)) {
      commit(() => markTabLoaded(path, "", `${basename(path)} is not a text file that can be edited here.`));
      return;
    }
    try {
      const snapshot = await readFileSnapshot(path, directory);
      if (snapshot != null) {
        const warning = largeFileReason(snapshot);
        if (warning) {
          commit(() => markTabLoaded(path, "", null, snapshot.contentHash, {
            largeFile: true,
            loadWarning: warning,
            sizeBytes: snapshot.sizeBytes,
            readOnly: snapshot.readOnly,
          }));
        } else {
          commit(() => markTabLoaded(path, snapshot.content, null, snapshot.contentHash, {
            largeFile: false,
            loadWarning: null,
            sizeBytes: snapshot.sizeBytes,
            readOnly: snapshot.readOnly,
          }));
        }
      } else {
        commit(() => markTabLoaded(path, "", `Could not read ${path}`));
      }
    } catch (error) {
      const message = errorMessage(error);
      if (isLargeFileError(message)) {
        commit(() => markTabLoaded(path, "", null, undefined, {
          largeFile: true,
          loadWarning: message,
          sizeBytes: undefined,
        }));
      } else {
        commit(() => markTabLoaded(path, "", message || `Could not read ${path}`));
      }
    }
  };

  const loadFileIfNeeded = (path: string) => {
    const currentState = useAppStore.getState();
    const tab = currentState.editorTabs.find((t) => editorPathsEqual(t.path, path, currentState.workingDirectory));
    if (tab?.loading) {
      void loadFileContent(path);
    }
  };

  const revealEditorTarget = (target: EditorTarget | null = pendingRevealRef.current) => {
    if (!target?.line || !editorPathsEqual(target.path, activeTabPath, workingDirectory)) return false;
    const currentState = useAppStore.getState();
    const tab = currentState.editorTabs.find((item) => editorPathsEqual(item.path, target.path, currentState.workingDirectory));
    if (!tab || tab.loading || tab.error || tab.largeFile || mdPreview) return false;
    const editor = editorRef.current;
    if (!editor) return false;
    const lineNumber = Math.max(1, Math.floor(target.line));
    const column = Math.max(1, Math.floor(target.column ?? 1));
    editor.setPosition?.({ lineNumber, column });
    editor.revealPositionInCenter?.({ lineNumber, column });
    editor.revealLineInCenter?.(lineNumber);
    editor.focus();
    setCursor({ line: lineNumber, column });
    if (editorPathsEqual(pendingRevealRef.current?.path, target.path, workingDirectory)) {
      pendingRevealRef.current = null;
    }
    return true;
  };

  const handleSetActive = (path: string, target?: EditorTarget) => {
    setActiveTab(path);
    if (target?.line) {
      const column = target.column ?? 1;
      pendingRevealRef.current = { path, line: target.line, column };
      setCursor({ line: target.line, column });
      window.setTimeout(() => revealEditorTarget({ path, line: target.line, column }), 0);
    } else {
      setCursor({ line: 1, column: 1 });
    }
    useAppStore.setState({ activeEditorPath: path });
  };

  const openFile = (targetPath: string) => {
    const normalized = targetPath.trim();
    if (!normalized) return;
    openEditorTab(normalized);
    loadFileIfNeeded(normalized);
    handleSetActive(normalized);
  };

  useEffect(() => {
    if (!pendingRevealRef.current) return;
    const id = window.setTimeout(() => {
      revealEditorTarget();
    }, 0);
    return () => window.clearTimeout(id);
  }, [activeTabPath, activeTab?.loading, activeTab?.error, activeTab?.largeFile, mdPreview]);

  const closeEditorPanel = () => {
    removePanel(editorSlotId);
  };

  const hideEditor = () => {
    if (chatSlot) focusPanel(chatSlot.id);
    else closeEditorPanel();
  };

  const save = async () => {
    if (!activeTab) {
      pushToast("当前未打开文件。", "info", 1600);
      return;
    }
    if (activeTab.readOnly) {
      pushToast("该文件由 MiniCode 生成，仅供只读查看。", "info", 2400);
      return;
    }
    if (activeTab.largeFile) {
      pushToast(`${basename(activeTab.path)} 未加载到编辑器中。`, "warning", 2400);
      return;
    }
    if (!dirty) {
      setSaveStatus("saved");
      pushToast(`${basename(activeTab.path)} 已保存。`, "info", 1400);
      window.setTimeout(() => setSaveStatus("idle"), 1000);
      return;
    }
    if (saving) return;
    setSaving(true);
    setSaveStatus("idle");

    const savePath = activeTab.path;
    const saveContent = activeTab.content;
    const saveOriginal = activeTab.original;
    const expectedHash = activeTab.contentHash ?? "";
    const saveWorkspace = workingDirectory;
    const saveEpochKey = loadEpochKey(savePath, saveWorkspace);
    const saveEpoch = (loadEpochRef.current.get(saveEpochKey) ?? 0) + 1;
    loadEpochRef.current.set(saveEpochKey, saveEpoch);
    const canCommitSave = () => {
      if (loadEpochRef.current.get(saveEpochKey) !== saveEpoch) return false;
      const state = useAppStore.getState();
      if (!workspaceRootsEqual(state.workingDirectory, saveWorkspace)) return false;
      const currentTab = state.editorTabs.find((tab) => editorPathsEqual(tab.path, savePath, state.workingDirectory));
      return Boolean(
        currentTab
        && currentTab.original === saveOriginal
        && (currentTab.contentHash ?? "") === expectedHash,
      );
    };
    try {
      // Route saves through the backend even in desktop mode. The agent and
      // editor then share one guarded mutation queue; native IPC remains for
      // reads/tree operations but cannot race a Python-side model edit here.
      const result = await compareWriteWorkspaceFile(savePath, expectedHash, saveContent, saveWorkspace);
      if (result.ok) {
        if (!canCommitSave()) return;
        const savedFile = result.file as { contentHash?: string; content_hash?: string };
        const nextHash = savedFile.contentHash ?? savedFile.content_hash;
        // Mark exactly the payload acknowledged by disk as the baseline. If
        // the user typed again while this request was in flight, current
        // content remains newer than original and the tab correctly stays dirty.
        markTabSaved(savePath, saveContent, nextHash);
        setSaveStatus("saved");
        pushToast(`已保存 ${basename(savePath)}`, "success", 1600);
        window.setTimeout(() => setSaveStatus("idle"), 1400);
      } else {
        if (!canCommitSave()) return;
        setSaveStatus("error");
        if (result.conflict) {
          markTabExternalChanged(savePath);
          pushToast(`${basename(savePath)} 已在磁盘上更改。为避免覆盖，已跳过保存。`, "warning", 4200);
        } else {
          pushToast(result.message || `保存失败：${basename(savePath)}`, "error", 3500);
        }
      }
    } finally {
      setSaving(false);
    }
  };

  const reloadFileFromDisk = async (
    path: string,
    { silent = false }: { silent?: boolean } = {},
  ) => {
    const stateAtRequest = useAppStore.getState();
    const tabAtRequest = stateAtRequest.editorTabs.find((tab) => editorPathsEqual(tab.path, path, stateAtRequest.workingDirectory));
    if (!tabAtRequest) return;
    const directory = stateAtRequest.workingDirectory;
    const epochKey = loadEpochKey(path, directory);
    const epoch = (loadEpochRef.current.get(epochKey) ?? 0) + 1;
    loadEpochRef.current.set(epochKey, epoch);
    const canCommit = () => {
      if (loadEpochRef.current.get(epochKey) !== epoch) return false;
      const currentState = useAppStore.getState();
      if (!workspaceRootsEqual(currentState.workingDirectory, directory)) return false;
      const currentTab = currentState.editorTabs.find((tab) => editorPathsEqual(tab.path, path, currentState.workingDirectory));
      return Boolean(
        currentTab
        && currentTab === tabAtRequest
        && currentTab.content === tabAtRequest.content
        && currentTab.original === tabAtRequest.original
        && currentTab.contentHash === tabAtRequest.contentHash,
      );
    };
    try {
      const snapshot = await readFileSnapshot(path, directory);
      if (snapshot == null) throw new Error(`无法读取 ${path}`);
      if (!canCommit()) return;
      reloadTab(path, snapshot.content, snapshot.contentHash);
      if (!silent) pushToast(`已从磁盘重新加载 ${basename(path)}。`, "success", 1800);
    } catch (error) {
      if (!canCommit()) return;
      markTabExternalChanged(path);
      if (!silent) pushToast(errorMessage(error) || `无法重新加载 ${basename(path)}。`, "error", 3500);
    }
  };

  // External changes follow the same contract as established editors: clean
  // buffers track disk automatically; dirty buffers keep user edits and expose
  // an explicit reload decision.
  const lastFileChangeLen = useRef(fileChanges.length);
  useEffect(() => {
    if (fileChanges.length <= lastFileChangeLen.current) {
      lastFileChangeLen.current = fileChanges.length;
      return;
    }
    const newChanges = fileChanges.slice(lastFileChangeLen.current);
    lastFileChangeLen.current = fileChanges.length;
    for (const change of newChanges) {
      const currentState = useAppStore.getState();
      const tab = currentState.editorTabs.find((candidate) =>
        editorPathsEqual(candidate.path, change.path, currentState.workingDirectory));
      if (!tab) continue;
      if (isImagePath(tab.path) || isPdfPath(tab.path)) continue;
      if (tab.content === tab.original && change.event !== "delete") {
        markTabExternalChanged(tab.path);
        void reloadFileFromDisk(tab.path, { silent: true });
      } else {
        markTabExternalChanged(tab.path);
      }
    }
    // reloadFileFromDisk intentionally reads the latest workingDirectory and
    // store snapshot; fileChanges is the event fence for this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileChanges, markTabExternalChanged]);

  const revert = () => {
    if (!activeTab || !dirty) return;
    updateTabContent(activeTab.path, activeTab.original);
  };

  const openActiveFileDiff = () => {
    if (!activeTabPath || !activeGitChange?.patch) return;
    setDiffReviewState({
      requestId: `editor-diff-${activeTabPath}`,
      toolName: "文件改动",
      diff: activeGitChange.patch,
      files: [{
        path: activeGitChange.path,
        patch: activeGitChange.patch,
        additions: activeGitChange.additions,
        deletions: activeGitChange.deletions,
      }],
      selectedPath: activeGitChange.path,
      status: "viewing",
      mode: "view",
      fileDecisions: {},
      lineComments: [],
    });
    setRightStackTab("diff");
  };

  const handleCloseTab = async (path: string) => {
    const tab = tabs.find((t) => editorPathsEqual(t.path, path, workingDirectory));
    if (tab && tab.content !== tab.original) {
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: "放弃更改",
        message: `确定放弃对 ${basename(path)} 的更改吗？`,
        confirmLabel: "放弃",
        danger: true,
      });
      if (!ok) return;
    }
    closeEditorTab(path);
  };

  const handleCloseTabs = async (paths: string[]) => {
    for (const path of paths) {
      await handleCloseTab(path);
    }
  };

  useEffect(() => {
    const handleInsert = (event: Event) => {
      const detail = (event as EditorInsertEvent).detail;
      if (!detail?.text || !activeTab || activeTab.loading || activeTab.error || activeTab.largeFile || activeTab.readOnly || mdPreview) return;
      const editor = editorRef.current;
      if (!editor) return;
      const selection = editor.getSelection();
      if (!selection) return;
      editor.executeEdits("chat-code-insert", [{
        range: selection,
        text: detail.text,
        forceMoveMarkers: true,
      }]);
      editor.focus();
      detail.handled = true;
    };
    window.addEventListener("editor:insert-text", handleInsert);
    return () => window.removeEventListener("editor:insert-text", handleInsert);
  }, [activeTab, mdPreview]);

  // Keyboard shortcut listeners (Ctrl+S, Ctrl+W)
  const saveRef = useRef(save);
  const closeTabRef = useRef(handleCloseTab);
  saveRef.current = save;
  closeTabRef.current = handleCloseTab;
  useEffect(() => {
    const handleSave = () => void saveRef.current();
    const handleCloseTabEvent = () => {
      if (activeTabPath) closeTabRef.current(activeTabPath);
    };
    window.addEventListener("editor:save", handleSave);
    window.addEventListener("editor:close-tab", handleCloseTabEvent);
    return () => {
      window.removeEventListener("editor:save", handleSave);
      window.removeEventListener("editor:close-tab", handleCloseTabEvent);
    };
  }, [activeTabPath]);

  return (
    <div className="flex-1 min-h-0 flex flex-col" style={{ background: "var(--surface-page)" }}>
      {showEditorTabs && tabs.length > 0 && (
        <div className="flex min-h-[38px] overflow-x-auto overflow-y-hidden gap-0.5 px-2.5 pt-1.5 pb-0 border-b scrollbar-thin" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-sidebar)", scrollbarColor: "color-mix(in oklch, var(--text-muted) 35%, transparent) transparent" }}>
          {tabs.map((tab) => {
            const tabDirty = tab.content !== tab.original;
            const active = editorPathsEqual(tab.path, activeTabPath, workingDirectory);
            return (
              <div
                key={tab.path}
                className="editor-tab relative inline-flex items-center gap-1.5 h-8 max-w-60 min-w-[124px] flex-none border border-transparent rounded-t-[7px] rounded-b-none cursor-pointer px-2.5 text-xs transition-[background,color,border-color] duration-100"
                onContextMenu={(e) => {
                  e.preventDefault();
                  setCtxMenu({ x: e.clientX, y: e.clientY, path: tab.path });
                }}
                title={tab.path}
                style={{
                  borderBottomColor: active ? "var(--surface-base)" : "transparent",
                  background: active ? "var(--surface-base)" : "transparent",
                  color: active ? "var(--text-primary)" : "var(--text-muted)",
                  fontFamily: "var(--font-ui)",
                }}
              >
                <button
                  type="button"
                  onClick={() => handleSetActive(tab.path)}
                  className="min-w-0 flex-1 inline-flex items-center gap-1.5 border-0 bg-transparent p-0 cursor-pointer"
                  style={{ color: "inherit", fontFamily: "inherit" }}
                  title={tab.path}
                >
                {tabDirty ? (
                  <Circle size={14} fill="currentColor" className="shrink-0" style={{ color: "var(--state-warning)" }} />
                ) : (
                  <span className="editor-tab-file-icon shrink-0" style={{ color: fileGlyphColor(tab.path) }} aria-hidden="true">
                    {fileIcon(tab.path, { size: 14, className: "editor-tab-file-icon-svg" })}
                  </span>
                )}
                <span className="overflow-hidden text-ellipsis whitespace-nowrap text-xs">
                  {basename(tab.path)}
                </span>
                </button>
                <button
                  type="button"
                  className="editor-tab-close inline-flex items-center justify-center rounded-[4px] w-[18px] h-[18px] shrink-0 ml-auto transition-[opacity,background] duration-100"
                  title="关闭标签页"
                  aria-label={`关闭 ${basename(tab.path)}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    handleCloseTab(tab.path);
                  }}
                  style={{
                    color: "var(--text-muted)",
                    borderRadius: "var(--radius-sm, 4px)",
                    opacity: active || tabDirty ? 1 : 0,
                  }}
                >
                  {tabDirty ? <Circle size={14} fill="currentColor" /> : <X size={14} />}
                </button>
              </div>
            );
          })}
        </div>
      )}
      {canRenderMarkdown && (
        <div className="flex items-center justify-between gap-2.5 min-h-[34px] px-2.5 py-[5px] border-b" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-page)" }}>
          <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>{basename(activeTab?.path ?? "Markdown")}</span>
          <div role="tablist" aria-label="Markdown 视图模式" className="inline-flex items-center gap-0.5 p-0.5 border rounded-[6px] shrink-0" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-soft)" }}>
            <button
              type="button"
              role="tab"
              aria-selected={!mdPreview}
              onClick={() => setMdPreview(false)}
              className="h-6 inline-flex items-center gap-[5px] px-2 border-0 rounded-[4px] cursor-pointer"
              style={{
                background: !mdPreview ? "var(--surface-raised)" : "transparent",
                color: !mdPreview ? "var(--text-primary)" : "var(--text-muted)",
                fontFamily: "var(--font-ui)",
                fontSize: "var(--text-xs)",
                fontWeight: !mdPreview ? 650 : 500,
                boxShadow: !mdPreview ? "var(--shadow-sm)" : "none",
              }}
            >
              <Edit3 size={14} />
              编辑
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mdPreview}
              onClick={() => setMdPreview(true)}
              className="h-6 inline-flex items-center gap-[5px] px-2 border-0 rounded-[4px] cursor-pointer"
              style={{
                background: mdPreview ? "var(--surface-raised)" : "transparent",
                color: mdPreview ? "var(--text-primary)" : "var(--text-muted)",
                fontFamily: "var(--font-ui)",
                fontSize: "var(--text-xs)",
                fontWeight: mdPreview ? 650 : 500,
                boxShadow: mdPreview ? "var(--shadow-sm)" : "none",
              }}
            >
              <Eye size={14} />
              预览
            </button>
          </div>
        </div>
      )}

      {activeTab?.externalChanged && !isPreviewableMediaPath(activeTab.path) && (
        <div
          className="min-h-9 px-3 py-1.5 flex items-center gap-2 border-b"
          style={{ borderColor: "var(--state-warning)", background: "var(--surface-soft)", color: "var(--text-secondary)", fontSize: "var(--text-xs)" }}
        >
          <FileWarning size={15} style={{ color: "var(--state-warning)" }} />
          <span className="flex-1 min-w-0">该文件已在磁盘上更改。</span>
          <button
            type="button"
            onClick={() => void reloadFileFromDisk(activeTab.path)}
            className="inline-flex items-center gap-1.5 px-2 py-1 border rounded-[4px] cursor-pointer"
            style={{ borderColor: "var(--border-subtle)", background: "var(--surface-raised)", color: "var(--text-primary)" }}
            title="放弃编辑器中的更改并从磁盘重新加载"
          >
            <RefreshCw size={13} />
            重新加载
          </button>
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-hidden flex flex-col" style={{ background: "var(--surface-base)" }}>
        {activeTab ? (
          activeTab.loading ? (
            <div className="h-full grid place-items-center" style={{ color: "var(--text-muted)" }}>
              正在加载文件...
            </div>
          ) : activeTab.error ? (
            <FileLoadErrorNotice path={activeTab.path} error={activeTab.error} onRetry={() => {
              useAppStore.setState((state) => ({
                editorTabs: state.editorTabs.map((tab) => editorPathsEqual(tab.path, activeTab.path, state.workingDirectory)
                  ? { ...tab, loading: true, error: null }
                  : tab),
              }));
              void loadFileContent(activeTab.path);
            }} />
          ) : isImagePath(activeTab.path) ? (
            <ImageViewer path={activeTab.path} workingDirectory={workingDirectory} />
          ) : isPdfPath(activeTab.path) ? (
            <PdfViewer path={activeTab.path} workingDirectory={workingDirectory} />
          ) : activeTab.largeFile ? (
            <LargeFileNotice tab={activeTab} />
          ) : canRenderMarkdown && mdPreview && markdownPreviewTooImageHeavy ? (
            <MarkdownPreviewLimitNotice
              imageCount={markdownImageCount}
              onEdit={() => setMdPreview(false)}
            />
          ) : canRenderMarkdown && mdPreview ? (
            <div className="md-prose editor-markdown-preview flex-1 overflow-y-auto px-[34px] py-6 text-base leading-[1.7] break-words" style={{ color: "var(--text-primary)" }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownPreviewComponents} urlTransform={markdownUrlTransform}>
                {activeTab.content}
              </ReactMarkdown>
            </div>
          ) : monacoUnavailable ? (
            <PlainTextEditor
              value={activeTab.content}
              onChange={(value) => updateTabContent(activeTab.path, value)}
              onCursorChange={setCursor}
              readOnly={activeTab.readOnly}
            />
          ) : (
            <Suspense fallback={<EditorLoading />}>
              <LazyMonacoEditor
                height="100%"
                language={language}
                theme={monacoTheme}
                path={activeTab.path}
                loading={<EditorLoading />}
                value={activeTab.content}
                onChange={(value) => updateTabContent(activeTab.path, value ?? "")}
                onMount={(editor) => {
                  monacoMountedRef.current = true;
                  editorRef.current = editor as MonacoEditorInstance;
                  (editor as MonacoEditorInstance).addAction?.({
                    id: "minicode.ask-about-selection",
                    label: "在侧边对话中询问所选内容",
                    contextMenuGroupId: "navigation",
                    contextMenuOrder: 1.5,
                    run: (mountedEditor) => {
                      const selection = mountedEditor.getSelection();
                      const text = selection
                        ? mountedEditor.getModel?.()?.getValueInRange(selection).trim() ?? ""
                        : "";
                      if (!text) {
                        pushToast("请先选择一些代码。", "warning");
                        return;
                      }
                      useAppStore.getState().openSideChatWithSelection(text, activeTab.path);
                    },
                  });
                  editor.onDidChangeCursorPosition((event) => {
                    setCursor({ line: event.position.lineNumber, column: event.position.column });
                  });
                  window.setTimeout(() => revealEditorTarget(), 0);
                }}
                options={{
                  automaticLayout: true,
                  readOnly: Boolean(activeTab.readOnly),
                  fontFamily: "var(--font-mono)",
                  fontSize: Math.round(15 * codeTextScale),
                  lineHeight: Math.round(23 * codeTextScale),
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  wordWrap: "on",
                  fontLigatures: true,
                  cursorBlinking: reducedMotion ? "solid" : "smooth",
                  cursorSmoothCaretAnimation: reducedMotion ? "off" : "on",
                  renderLineHighlight: "all",
                  renderWhitespace: "selection",
                  roundedSelection: false,
                  padding: { top: 18, bottom: 24 },
                  lineNumbersMinChars: 4,
                  lineDecorationsWidth: 12,
                  renderFinalNewline: "dimmed",
                  folding: true,
                  glyphMargin: false,
                  bracketPairColorization: { enabled: true },
                  guides: { indentation: true, bracketPairs: true },
                  smoothScrolling: !reducedMotion,
                  stickyScroll: { enabled: false },
                  scrollbar: {
                    verticalScrollbarSize: 12,
                    horizontalScrollbarSize: 12,
                    useShadows: false,
                    alwaysConsumeMouseWheel: false,
                  },
                  overviewRulerBorder: false,
                  hideCursorInOverviewRuler: true,
                }}
              />
            </Suspense>
          )
        ) : (
          <div className="h-full flex flex-col items-center justify-center gap-2.5 text-sm" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
            <span className="editor-empty-file-icon" aria-hidden="true"><FileCode2 size={28} strokeWidth={1.8} className="editor-empty-file-icon-svg" /></span>
            <div className="font-semibold" style={{ color: "var(--text-secondary)" }}>未打开文件</div>
            <div className="max-w-[420px] text-center">
              从左侧项目文件或搜索中打开工作区文件。
            </div>
          </div>
        )}
      </div>

      <div title={activeTabPath ?? ""} className="flex gap-3 min-h-6 items-center px-3 border-t overflow-hidden whitespace-nowrap text-xs" style={{ color: dirty ? "var(--state-warning)" : "var(--text-muted)", borderColor: "var(--border-subtle)", fontFamily: "var(--font-mono)", background: "var(--surface-sidebar)" }}>
        <span className="flex-1 min-w-0 overflow-hidden text-ellipsis">
          {activeTabPath || "未打开文件"}{dirty ? " - 已修改" : ""}
        </span>
        {activeTab?.sizeBytes != null && <span>{formatBytes(activeTab.sizeBytes)}</span>}
        {activeTab?.readOnly && (
          <span className="inline-flex items-center gap-1" title="MiniCode 生成的只读工具结果">
            <LockKeyhole size={14} /> 只读
          </span>
        )}
        <span>{language}</span>
        {activeGitChange?.patch && (
          <button
            type="button"
            onClick={openActiveFileDiff}
            className="inline-flex items-center gap-1 border-0 rounded-[4px] cursor-pointer"
            style={{
              height: 22,
              padding: "0 7px",
              background: "color-mix(in oklch, var(--accent-primary) 10%, transparent)",
              color: "var(--accent-primary)",
              fontFamily: "var(--font-ui)",
              fontSize: "var(--text-xs)",
              fontWeight: "var(--fw-semibold)",
            }}
          >
            <GitCompare size={14} />
            Diff
            <span style={{ color: "var(--state-success)" }}>+{activeGitChange.additions}</span>
            <span style={{ color: "var(--state-danger)" }}>-{activeGitChange.deletions}</span>
          </button>
        )}
        {saveStatus === "saved" && <span style={{ color: "var(--state-success)" }}>已保存</span>}
        {saveStatus === "error" && <span style={{ color: "var(--state-danger)" }}>保存失败</span>}
        <span>{`第 ${cursor.line} 行，第 ${cursor.column} 列`}</span>
      </div>

      {ctxMenu && (
        <ContextMenu
          position={{ x: ctxMenu.x, y: ctxMenu.y }}
          onClose={() => setCtxMenu(null)}
          items={[
            { label: "关闭标签页", onClick: () => handleCloseTab(ctxMenu.path) },
            {
              label: "关闭其他标签页",
              onClick: () => {
                const paths = tabs.filter((tab) => !editorPathsEqual(tab.path, ctxMenu.path, workingDirectory)).map((tab) => tab.path);
                void handleCloseTabs(paths);
              },
            },
            { label: "关闭所有标签页", onClick: () => { void handleCloseTabs(tabs.map((tab) => tab.path)); } },
            {
              label: "关闭右侧标签页",
              onClick: () => {
                const idx = tabs.findIndex((t) => editorPathsEqual(t.path, ctxMenu.path, workingDirectory));
                void handleCloseTabs(tabs.slice(idx + 1).map((tab) => tab.path));
              },
            },
            { separator: true, label: "" },
            {
              label: "复制文件路径",
              onClick: () => {
                const absolutePath = resolveDesktopFsPath(ctxMenu.path, workingDirectory);
                void navigator.clipboard.writeText(absolutePath).then(
                  () => pushToast("文件路径已复制。", "success", 1600),
                  (error) => pushToast(`复制文件路径失败：${errorMessage(error)}`, "error", 3000),
                );
              },
            },
            // Revealing a path needs an OS shell; in browser mode the entry did
            // nothing at all, so it is not offered there.
            ...(isDesktop() ? [{
              label: "在文件管理器中显示",
              onClick: () => { void revealPath(resolveDesktopFsPath(ctxMenu.path, workingDirectory)); },
            }] : []),
          ]}
        />
      )}
    </div>
  );
};

const EditorLoading = () => (
  <div className="h-full grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
    正在加载编辑器...
  </div>
);

const PlainTextEditor = ({ value, onChange, onCursorChange, readOnly = false }: PlainTextEditorProps) => {
  const updateCursor = (target: HTMLTextAreaElement) => {
    onCursorChange(cursorFromOffset(target.value, target.selectionStart ?? 0));
  };
  return (
    <textarea
      className="editor-plain-textarea"
      aria-label="纯文本编辑器"
      value={value}
      readOnly={readOnly}
      spellCheck={false}
      onChange={(event) => {
        onChange(event.currentTarget.value);
        updateCursor(event.currentTarget);
      }}
      onClick={(event) => updateCursor(event.currentTarget)}
      onKeyUp={(event) => updateCursor(event.currentTarget)}
      onSelect={(event) => updateCursor(event.currentTarget)}
      style={plainTextEditorStyle}
    />
  );
};

const plainTextEditorStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  width: "100%",
  height: "100%",
  resize: "none",
  border: 0,
  outline: "none",
  padding: "18px 22px",
  boxSizing: "border-box",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--code-font-size)",
  lineHeight: "calc(23px * var(--code-text-scale))",
  whiteSpace: "pre",
  overflow: "auto",
  tabSize: 2,
};

const LargeFileNotice = ({ tab }: { tab: { path: string; loadWarning?: string | null; sizeBytes?: number } }) => (
  <div className="h-full flex flex-col items-center justify-center gap-[9px] p-6 text-center" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
    <FileWarning size={28} style={{ color: "var(--state-warning)" }} />
    <div className="font-bold" style={{ color: "var(--text-primary)" }}>文件未加载到编辑器</div>
    <div className="max-w-[520px] leading-[1.5]" style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)" }}>
      {tab.loadWarning || "该文件过大，无法在编辑器中安全呈现。"}
    </div>
    <div className="max-w-[520px] overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
      {basename(tab.path)}{tab.sizeBytes != null ? ` - ${formatBytes(tab.sizeBytes)}` : ""}
    </div>
  </div>
);

const FileLoadErrorNotice = ({ path, error, onRetry }: { path: string; error: string; onRetry: () => void }) => (
  <div className="h-full flex flex-col items-center justify-center gap-[9px] p-6 text-center" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
    <FileWarning size={28} style={{ color: "var(--state-danger)" }} />
    <div className="font-bold" style={{ color: "var(--text-primary)" }}>无法加载文件</div>
    <div className="max-w-[560px] leading-[1.5]" style={{ color: "var(--state-danger)", fontSize: "var(--text-sm)" }}>
      {error}
    </div>
    <div className="max-w-[560px] overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
      {path}
    </div>
    <button type="button" onClick={onRetry} className="inline-flex items-center gap-1.5 h-[30px] px-2.5 border rounded-[4px] cursor-pointer font-semibold" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-raised)", color: "var(--text-primary)", fontFamily: "var(--font-ui)", fontSize: "var(--text-xs)" }}>
      重试
    </button>
  </div>
);

const MarkdownPreviewLimitNotice = ({ imageCount, onEdit }: { imageCount: number; onEdit: () => void }) => (
  <div className="h-full flex flex-col items-center justify-center gap-[9px] p-6 text-center" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
    <Image size={28} style={{ color: "var(--state-warning)" }} />
    <div className="font-bold" style={{ color: "var(--text-primary)" }}>已跳过 Markdown 预览</div>
    <div className="max-w-[520px] leading-[1.5]" style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)" }}>
      此 Markdown 文件引用了 {imageCount.toLocaleString()} 张图片。请在编辑模式中打开，避免一次加载全部图片。
    </div>
    <button type="button" onClick={onEdit} className="inline-flex items-center gap-1.5 h-[30px] px-2.5 border rounded-[4px] cursor-pointer font-semibold" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-raised)", color: "var(--text-primary)", fontFamily: "var(--font-ui)", fontSize: "var(--text-xs)" }}>
      <Edit3 size={14} />
      编辑 Markdown
    </button>
  </div>
);

const ImageViewer = ({ path, workingDirectory }: { path: string; workingDirectory: string }) => {
  const imgSrc = rawFileUrl(path, workingDirectory);
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [imgSrc]);

  return (
    <div className="flex-1 min-h-0 flex items-center justify-center p-6 overflow-auto" style={{ background: "var(--surface-base)" }}>
      {failed ? (
        <div className="flex flex-col items-center gap-2" style={{ color: "var(--text-muted)" }}>
          <Image size={32} />
          <span style={{ fontSize: "var(--text-sm)" }}>无法显示图片</span>
          <span style={{ fontSize: "var(--text-sm)", fontFamily: "var(--font-ui)" }}>{basename(path)}</span>
        </div>
      ) : (
        <img
          src={imgSrc}
          alt={basename(path)}
          className="block max-w-full max-h-full object-contain rounded-[4px]"
          style={{ width: "auto", height: "auto" }}
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
};

const PdfViewer = ({ path, workingDirectory }: { path: string; workingDirectory: string }) => {
  const src = rawFileUrl(path, workingDirectory);
  return (
    <div className="flex-1 min-h-0 flex flex-col" style={{ background: "var(--surface-base)" }}>
      <div className="flex items-center gap-2 min-h-[34px] px-3 border-b" style={{ borderColor: "var(--border-subtle)", color: "var(--text-muted)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)" }}>
        <span className="editor-media-file-icon" style={{ color: fileGlyphColor(path) }} aria-hidden="true">{fileIcon(path, { size: 14, className: "editor-media-file-icon-svg" })}</span>
        <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">{basename(path)}</span>
      </div>
      <iframe
        title={basename(path)}
        src={src}
        sandbox="allow-scripts allow-same-origin"
        referrerPolicy={EDITOR_FRAME_REFERRER_POLICY}
        style={{
          flex: 1,
          minHeight: 0,
          width: "100%",
          border: 0,
          background: "var(--surface-base)",
        }}
      />
    </div>
  );
};

// TabContextMenu used to be defined inline here; now replaced by the
// generic ContextMenu component in ../components/ContextMenu.
