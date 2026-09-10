import { useEffect, useMemo, useRef, useState } from "react";
import { lazy, Suspense } from "react";
import { Circle, Edit3, Eye, FileCode2, FileWarning, GitCompare, Image, LockKeyhole, RefreshCw, X } from "lucide-react";
import { fileGlyphColor, fileIcon } from "../shell/fileTreeHelpers";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import { useAppStore } from "../stores";
import type { EditorOpenRequest, EditorTab } from "../stores/types";
import { editorPathComparisonKey, editorPathsEqual, editorStateForWorkspace } from "../stores/shared-helpers";
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
import {
  createMarkdownHeadingIdAssigner,
  decodeMarkdownFragment,
  markdownHeadingSlug,
} from "../lib/markdown";

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

const LazyPdfPreview = lazy(() => import("./PdfAttachmentPreview").then((module) => ({ default: module.PdfAttachmentPreview })));

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
  onDidDispose: (handler: () => void) => unknown;
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
const dirname = (path: string) => {
  const normalized = normalizeWorkspacePath(path);
  return normalized.slice(0, normalized.lastIndexOf("/") + 1);
};

const workspaceRelativePath = (path: string, workingDirectory: string): string => {
  const normalized = normalizeWorkspacePath(path);
  const root = normalizeWorkspacePath(workingDirectory);
  if (root && workspacePathWithin(normalized, root)) {
    return normalized.slice(root.length).replace(/^\/+/, "");
  }
  return normalized.replace(/^\.\/+/, "");
};

const resolveUnqualifiedEditorPath = async (path: string, workingDirectory: string, exactPath = false): Promise<string> => {
  const relative = workspaceRelativePath(path, workingDirectory);
  if (exactPath || !relative || relative.includes("/") || !workingDirectory.trim()) return relative || path;

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
  const normalized = normalizeWorkspacePath(path);
  const root = normalizeWorkspacePath(workingDirectory);
  if (!normalized || !root) return normalized;
  if (workspacePathsEqual(normalized, root)) return ".";
  if (workspacePathWithin(normalized, root)) {
    return normalized.slice(root.length).replace(/^\/+/, "") || ".";
  }
  return normalized;
};

const rawFileUrl = (path: string, workingDirectory: string, version: number): string => {
  if (!path || /^(https?:|data:|blob:|mailto:|tel:|#)/i.test(path)) return path;
  const normalized = toWorkspaceDisplayPath(path, workingDirectory);
  const url = new URL(workspaceRawResourceUrlWithToken(normalized, workingDirectory));
  url.searchParams.set("version", String(version));
  return url.toString();
};

const useRawFileUrl = (path: string, workingDirectory: string): string => {
  const version = useAppStore((state) => {
    for (let i = state.fileChanges.length - 1; i >= 0; i--) {
      const change = state.fileChanges[i];
      if (editorPathsEqual(change.path, path, workingDirectory)) return change.sequence;
    }
    return 0;
  });
  return useMemo(() => rawFileUrl(path, workingDirectory, version), [path, workingDirectory, version]);
};

const isAbsoluteLocalPath = (path: string): boolean =>
  /^[a-zA-Z]:(?:[\\/]|%5[cC]|%2[fF])/.test(path) || path.startsWith("/") || path.startsWith("\\");

const markdownUrlTransform = (url: string): string => {
  if (/^file:/i.test(url)) return URL.canParse(url) ? url : "";
  return isAbsoluteLocalPath(url) ? url : defaultUrlTransform(url);
};

const resolveWorkspaceAsset = (src: string, ownerPath: string, workingDirectory: string): { path: string; fragment: string } => {
  const trimmed = src.trim();
  if (!trimmed || /^(https?:|data:|blob:|mailto:|tel:|#)/i.test(trimmed)) return { path: trimmed, fragment: "" };
  const fragmentIndex = trimmed.indexOf("#");
  const fragment = fragmentIndex < 0 ? "" : trimmed.slice(fragmentIndex);
  let path = trimmed.split(/[?#]/, 1)[0];
  if (/^file:/i.test(path)) {
    const url = new URL(path);
    path = url.hostname && url.hostname !== "localhost" ? `//${url.hostname}${url.pathname}` : url.pathname.replace(/^\/([a-zA-Z]:\/)/, "$1");
  }
  // Decode the Markdown URL once. Tree/editor paths are already literal file
  // names, and a filename containing "%20" must not be decoded a second time.
  try {
    path = decodeURIComponent(path);
  } catch (error) {
    if (!(error instanceof URIError)) throw error;
    // A malformed escape is literal filename text, as in Codex local links.
  }
  if (isAbsoluteLocalPath(path)) return { path: toWorkspaceDisplayPath(path, workingDirectory), fragment };
  const ownerDir = dirname(ownerPath);
  const base = ownerDir || workingDirectory || "";
  return { path: toWorkspaceDisplayPath(base ? `${base}/${path}` : path, workingDirectory), fragment };
};

const resolveDesktopFsPath = (path: string, workingDirectory: string): string => {
  const trimmed = path.trim();
  if (!trimmed || isAbsoluteLocalPath(trimmed) || !workingDirectory.trim()) return trimmed;
  return normalizeWorkspacePath(`${workingDirectory}/${trimmed}`);
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
  headingId: ReturnType<typeof createMarkdownHeadingIdAssigner>,
) => {
  const heading = (level: 1 | 2 | 3) => (props: React.HTMLAttributes<HTMLHeadingElement> & {
    node?: { position?: { start?: { line?: number } } };
  }) => {
    const id = headingId(reactNodeText(props.children), props.node?.position?.start?.line);
    const Tag: "h1" | "h2" | "h3" = level === 1 ? "h1" : level === 2 ? "h2" : "h3";
    return <Tag {...props} id={id} style={{ scrollMarginTop: 16, ...props.style }} tabIndex={-1} />;
  };
  return {
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
    const href = typeof props.href === "string" ? props.href : "";
    const asset = resolveWorkspaceAsset(href, ownerPath, workingDirectory);
    const resourceUrl = useRawFileUrl(asset.path, workingDirectory);
    if (href.startsWith("#")) {
      const target = `${scopeId}-${markdownHeadingSlug(decodeMarkdownFragment(href.slice(1)))}`;
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
        href={href ? `${resourceUrl}${asset.fragment}` : props.href}
        target="_blank"
        rel="noreferrer"
        style={{ color: "var(--accent-primary)" }}
      />
    );
  },
  img: (props: React.ImgHTMLAttributes<HTMLImageElement>) => {
    const asset = resolveWorkspaceAsset(props.src ?? "", ownerPath, workingDirectory);
    const src = useRawFileUrl(asset.path, workingDirectory);
    return (
      <img
        {...props}
        src={`${src}${asset.fragment}`}
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

  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const savingPathsRef = useRef(new Set<string>());
  const [saveStatus, setSaveStatus] = useState<"idle" | "saved" | "error">("idle");
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; path: string } | null>(null);
  const [mdPreview, setMdPreview] = useState(false);
  const [monacoUnavailable, setMonacoUnavailable] = useState(false);
  const editorRef = useRef<MonacoEditorInstance | null>(null);
  const monacoMountedRef = useRef(false);
  const pendingRevealRef = useRef<EditorTarget | null>(null);
  const loadEpochRef = useRef(new Map<string, number>());
  const loadingTabsRef = useRef(new WeakSet<EditorTab>());
  const openingRequestsRef = useRef(new WeakSet<EditorOpenRequest>());

  const activeTab = tabs.find((tab) => editorPathsEqual(tab.path, activeTabPath, workingDirectory)) ?? null;
  const markdownScopeId = useMemo(
    () => `editor-markdown-${Math.abs(editorPathComparisonKey(activeTab?.path ?? "markdown", workingDirectory).split("").reduce((hash, char) => ((hash * 31) + char.charCodeAt(0)) | 0, 0))}`,
    [activeTab?.path, workingDirectory],
  );
  const markdownHeadingId = useMemo(
    () => createMarkdownHeadingIdAssigner(markdownScopeId),
    [markdownScopeId],
  );
  markdownHeadingId.reset();
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
    () => createMarkdownPreviewComponents(activeTab?.path ?? "", workingDirectory, markdownScopeId, markdownHeadingId),
    [activeTab?.path, activeTab?.content, workingDirectory, markdownScopeId, markdownHeadingId],
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
    setSaveStatus("idle");
  }, [activeTabPath, workingDirectory]);

  useEffect(() => {
    if (saveStatus !== "saved") return;
    const timer = window.setTimeout(() => setSaveStatus("idle"), 1400);
    return () => window.clearTimeout(timer);
  }, [saveStatus]);

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
      if (openingRequestsRef.current.has(request)) continue;
      openingRequestsRef.current.add(request);
      void resolveUnqualifiedEditorPath(request.path, workingDirectory, request.exact).then((resolvedPath) => {
        const state = useAppStore.getState();
        if (!state.editorOpenRequests.includes(request)) return;
        const activate = state.activeEditorOpenRequestId === request.id;
        openEditorTab(resolvedPath, { activate: false });
        if (activate) {
          handleSetActive(resolvedPath, { path: resolvedPath, line: request.line, column: request.column });
        }
        consumeEditorOpenRequest(request.id);
      }).catch((error: unknown) => {
        if (!useAppStore.getState().editorOpenRequests.includes(request)) return;
        consumeEditorOpenRequest(request.id);
        pushToast(`无法定位文件：${errorMessage(error)}`, "error", 5000);
      });
    }
  }, [editorOpenRequests, consumeEditorOpenRequest, openEditorTab, workingDirectory]);

  useEffect(() => {
    for (const tab of tabs) {
      if (tab.loading && !loadingTabsRef.current.has(tab)) {
        loadingTabsRef.current.add(tab);
        void loadFileContent(tab);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs, workingDirectory]);

  const applyFileSnapshot = (path: string, snapshot: FileSnapshot) => {
    const warning = largeFileReason(snapshot);
    markTabLoaded(path, warning ? "" : snapshot.content, null, snapshot.contentHash, {
      largeFile: Boolean(warning),
      loadWarning: warning,
      sizeBytes: snapshot.sizeBytes,
      readOnly: snapshot.readOnly,
    });
    return warning;
  };

  const loadFileContent = async (tab: EditorTab) => {
    const path = tab.path;
    const directory = workingDirectory;
    const epochKey = tab.id;
    const epoch = (loadEpochRef.current.get(epochKey) ?? 0) + 1;
    loadEpochRef.current.set(epochKey, epoch);
    const commit = (callback: () => void) => {
      if (loadEpochRef.current.get(epochKey) !== epoch) return;
      if (!workspaceRootsEqual(directory, useAppStore.getState().workingDirectory)) return;
      const currentState = useAppStore.getState();
      if (!currentState.editorTabs.includes(tab)) return;
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
        commit(() => applyFileSnapshot(path, snapshot));
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
      return;
    }
    const savePath = activeTab.path;
    const saveContent = activeTab.content;
    const saveOriginal = activeTab.original;
    const expectedHash = activeTab.contentHash ?? "";
    const saveWorkspace = workingDirectory;
    const saveEpochKey = activeTab.id;
    if (savingPathsRef.current.has(saveEpochKey)) return;
    savingPathsRef.current.add(saveEpochKey);
    setSaveStatus("idle");
    const saveEpoch = (loadEpochRef.current.get(saveEpochKey) ?? 0) + 1;
    loadEpochRef.current.set(saveEpochKey, saveEpoch);
    try {
      // Route saves through the backend even in desktop mode. The agent and
      // editor then share one guarded mutation queue; native IPC remains for
      // reads/tree operations but cannot race a Python-side model edit here.
      const result = await compareWriteWorkspaceFile(savePath, expectedHash, saveContent, saveWorkspace);
      const state = useAppStore.getState();
      const workspace = workspaceRootsEqual(state.workingDirectory, saveWorkspace)
        ? state : editorStateForWorkspace(saveWorkspace);
      const currentTab = workspace.editorTabs.find((tab) => tab.id === saveEpochKey);
      if (!currentTab || currentTab.original !== saveOriginal || (currentTab.contentHash ?? "") !== expectedHash
        || loadEpochRef.current.get(saveEpochKey) !== saveEpoch) return;
      const currentPath = currentTab.path;
      const saveIsVisible = workspaceRootsEqual(state.workingDirectory, saveWorkspace)
        && editorPathsEqual(state.activeTabPath, currentPath, saveWorkspace);
      if (result.ok) {
        // Mark exactly the payload acknowledged by disk as the baseline. If
        // the user typed again while this request was in flight, current
        // content remains newer than original and the tab correctly stays dirty.
        markTabSaved(currentPath, saveContent, result.file.content_hash, result.file.size_bytes ?? result.file.size, saveWorkspace);
        if (currentTab.externalChanged && workspaceRootsEqual(state.workingDirectory, saveWorkspace)) {
          void reloadFileFromDisk(currentPath, { silent: true, preserveEdits: true });
        }
        if (saveIsVisible) {
          setSaveStatus("saved");
          pushToast(`已保存 ${basename(currentPath)}`, "success", 1600);
        }
      } else {
        if (saveIsVisible) setSaveStatus("error");
        if (result.conflict) {
          markTabExternalChanged(currentPath, { workspaceRoot: saveWorkspace });
          if (saveIsVisible) pushToast(`${basename(currentPath)} 已在磁盘上更改。为避免覆盖，已跳过保存。`, "warning", 4200);
        } else if (saveIsVisible) {
          pushToast(result.message || `保存失败：${basename(currentPath)}`, "error", 3500);
        }
      }
    } finally {
      savingPathsRef.current.delete(saveEpochKey);
    }
  };

  const previousWorkspaceRef = useRef(workingDirectory);
  useEffect(() => {
    if (workspaceRootsEqual(previousWorkspaceRef.current, workingDirectory)) return;
    previousWorkspaceRef.current = workingDirectory;
    for (const tab of tabs) {
      if (tab.loading || isPreviewableMediaPath(tab.path)
        || savingPathsRef.current.has(tab.id)) continue;
      void reloadFileFromDisk(tab.path, { silent: true, preserveEdits: true });
    }
    // Reload cached buffers once when their workspace becomes visible again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workingDirectory]);

  const reloadFileFromDisk = async (
    path: string,
    { silent = false, preserveEdits = false }: { silent?: boolean; preserveEdits?: boolean } = {},
  ) => {
    const stateAtRequest = useAppStore.getState();
    const tabAtRequest = stateAtRequest.editorTabs.find((tab) => editorPathsEqual(tab.path, path, stateAtRequest.workingDirectory));
    if (!tabAtRequest) return;
    const directory = stateAtRequest.workingDirectory;
    const epochKey = tabAtRequest.id;
    const epoch = (loadEpochRef.current.get(epochKey) ?? 0) + 1;
    loadEpochRef.current.set(epochKey, epoch);
    const canCommit = () => {
      if (loadEpochRef.current.get(epochKey) !== epoch) return false;
      const currentState = useAppStore.getState();
      if (!workspaceRootsEqual(currentState.workingDirectory, directory)) return false;
      const currentTab = currentState.editorTabs.find((tab) => editorPathsEqual(tab.path, path, currentState.workingDirectory));
      return Boolean(
        currentTab
        && currentTab.id === tabAtRequest.id
        && (preserveEdits || currentTab === tabAtRequest)
        && currentTab.original === tabAtRequest.original
        && currentTab.contentHash === tabAtRequest.contentHash,
      );
    };
    try {
      const snapshot = await readFileSnapshot(path, directory);
      if (snapshot == null) throw new Error(`无法读取 ${path}`);
      if (!canCommit()) return;
      const currentTab = useAppStore.getState().editorTabs.find((tab) => editorPathsEqual(tab.path, path, directory))!;
      if (preserveEdits && currentTab.content !== currentTab.original) {
        markTabExternalChanged(path, { changed: snapshot.content !== currentTab.original });
        return;
      }
      const warning = applyFileSnapshot(path, snapshot);
      if (!silent) pushToast(warning || `已从磁盘重新加载 ${basename(path)}。`, warning ? "warning" : "success", warning ? 3500 : 1800);
    } catch (error) {
      if (!canCommit()) return;
      const message = errorMessage(error);
      const currentTab = useAppStore.getState().editorTabs.find((tab) => editorPathsEqual(tab.path, path, directory))!;
      if (isLargeFileError(message) && (!preserveEdits || currentTab.content === currentTab.original)) {
        markTabLoaded(path, "", null, undefined, { largeFile: true, loadWarning: message });
      } else {
        markTabExternalChanged(path);
      }
      if (!silent) pushToast(message || `无法重新加载 ${basename(path)}。`, "error", 3500);
    }
  };

  // External changes follow the same contract as established editors: clean
  // buffers track disk automatically; dirty buffers keep user edits and expose
  // an explicit reload decision.
  const lastFileChangeSequence = useRef(0);
  useEffect(() => {
    const latestSequence = fileChanges.at(-1)?.sequence ?? 0;
    if (latestSequence < lastFileChangeSequence.current) {
      lastFileChangeSequence.current = latestSequence;
      return;
    }
    if (latestSequence === lastFileChangeSequence.current) return;
    const newChanges = fileChanges.filter((change) => change.sequence > lastFileChangeSequence.current);
    lastFileChangeSequence.current = latestSequence;
    const changedPaths = new Map(newChanges.map((change) => [editorPathComparisonKey(change.path, workingDirectory), change]));
    for (const change of changedPaths.values()) {
      const currentState = useAppStore.getState();
      const tab = currentState.editorTabs.find((candidate) =>
        editorPathsEqual(candidate.path, change.path, currentState.workingDirectory));
      if (!tab) continue;
      // Media viewers subscribe to the changed path through useRawFileUrl.
      if (isImagePath(tab.path) || isPdfPath(tab.path)) continue;
      if (tab.loading || savingPathsRef.current.has(tab.id)) {
        markTabExternalChanged(tab.path);
        continue;
      }
      if (change.event === "delete") {
        markTabExternalChanged(tab.path);
      } else {
        void reloadFileFromDisk(tab.path, { silent: true, preserveEdits: true });
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
    const state = useAppStore.getState();
    if (!workspaceRootsEqual(state.workingDirectory, workingDirectory)) return false;
    const tab = state.editorTabs.find((t) => editorPathsEqual(t.path, path, workingDirectory));
    if (tab && tab.content !== tab.original) {
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: "放弃更改",
        message: `确定放弃对 ${basename(path)} 的更改吗？`,
        confirmLabel: "放弃",
        danger: true,
      });
      if (!ok) return false;
      const current = useAppStore.getState();
      if (!workspaceRootsEqual(current.workingDirectory, workingDirectory)
        || !current.editorTabs.includes(tab)) return false;
    }
    closeEditorTab(path);
    return true;
  };

  const handleCloseTabs = async (paths: string[]) => {
    for (const path of paths) {
      if (!await handleCloseTab(path)) break;
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
                key={normalizeWorkspaceRoot(workingDirectory)}
                height="100%"
                language={language}
                theme={monacoTheme}
                path={`minicode-editor://buffer/${activeTab.id}`}
                loading={<EditorLoading />}
                value={activeTab.content}
                onChange={(value) => updateTabContent(activeTab.path, value ?? "")}
                onMount={(editor) => {
                  monacoMountedRef.current = true;
                  editorRef.current = editor as MonacoEditorInstance;
                  editor.onDidDispose(() => {
                    if (editorRef.current === editor) editorRef.current = null;
                  });
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
                      const state = useAppStore.getState();
                      state.openSideChatWithSelection(text, state.activeTabPath ?? undefined);
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
  const imgSrc = useRawFileUrl(path, workingDirectory);
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
  const src = useRawFileUrl(path, workingDirectory);
  return (
    <Suspense fallback={<EditorLoading />}>
      <LazyPdfPreview url={src} name={basename(path)} />
    </Suspense>
  );
};

// TabContextMenu used to be defined inline here; now replaced by the
// generic ContextMenu component in ../components/ContextMenu.
