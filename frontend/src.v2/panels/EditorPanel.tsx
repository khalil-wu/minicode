import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { lazy, Suspense } from "react";
import { ArrowLeft, ArrowRight, Braces, ChevronRight, Circle, FileCode2, FileWarning, FolderTree, GitCompare, Image, LockKeyhole, MessageSquare, RefreshCw, Sparkles, X } from "lucide-react";
import { fileGlyphColor, fileIcon } from "../lib/file-icons";
import { defaultUrlTransform } from "react-markdown";
import { useAppStore } from "../stores";
import type { EditorOpenRequest, EditorTab } from "../stores/types";
import { editorPathComparisonKey, editorPathsEqual, editorStateForWorkspace, loadEditorViewState, persistEditorViewState } from "../stores/shared-helpers";
import { workspaceRawResourceUrlWithToken } from "../protocol/api";
import {
  compareWriteWorkspaceFile,
  readWorkspaceFile,
  searchWorkspaceFiles,
} from "../protocol/workspace";
import { fsReadFileInfo, fsSearchFiles, isDesktop, revealPath } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { SelectMenu } from "../components/SelectMenu";
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
import { editorFileLimitReason } from "../lib/editor-file-policy";
import { withPreviewCacheBust } from "../lib/artifact-resource";
import {
  createMarkdownHeadingIdAssigner,
  decodeMarkdownFragment,
  markdownHeadingSlug,
} from "../lib/markdown";
import { useAgentEditReview } from "./useAgentEditReview";
import { AgentEditReviewBar } from "../components/AgentEditReviewBar";
import type { EditorTextSurface } from "./editor-text-surface";
import type { MarkdownEditorSession } from "./LiveMarkdownEditor";
import { EditorActions, type EditorAction } from "./EditorActions";
import { FileTree } from "../shell/FileTree";
import { defineMiniCodeMonacoTheme, miniCodeMonacoThemeName } from "./monacoTheme";
import { configureMiniCodeMonacoWorkers, editorModelUri, loadMiniCodeLanguageServices, registerMiniCodeEditorOpener } from "./monacoLanguageServices";
import { renameMonacoModel } from "./monacoModelRename";
import { useWorkspaceModelIndex } from "./useWorkspaceModelIndex";
import { registerWorkspaceEditorSurface, retainsWorkspaceBufferEditModel } from "./applyWorkspaceBufferEdits";
import { miniCodeCodeEditingOptions } from "./monacoEditorFeatures";
import "./EditorChrome.css";
import { EditorNavigation } from "./editorNavigation";
import { formatEditorModel } from "./editorNativeServices";
const LazyEditorSymbols = lazy(() => import("./EditorSymbols").then((module) => ({ default: module.EditorSymbols })));

const LazyMonacoEditor = lazy(async () => {
  configureMiniCodeMonacoWorkers();
  const [reactMonaco, monaco] = await Promise.all([
    import("@monaco-editor/react"),
    import("monaco-editor/editor/editor.api.js"),
    loadMiniCodeLanguageServices(),
    import("monaco-editor/languages/definitions/typescript/register.js"),
    import("monaco-editor/languages/definitions/javascript/register.js"),
    import("monaco-editor/languages/definitions/css/register.js"),
    import("monaco-editor/languages/definitions/scss/register.js"),
    import("monaco-editor/languages/definitions/less/register.js"),
    import("monaco-editor/languages/definitions/html/register.js"),
    import("monaco-editor/languages/definitions/markdown/register.js"),
    import("monaco-editor/languages/definitions/python/register.js"),
    import("monaco-editor/languages/definitions/yaml/register.js"),
  ]);
  reactMonaco.loader?.config?.({ monaco });
  return { default: reactMonaco.default };
});

const LazyPdfPreview = lazy(() => import("./PdfAttachmentPreview").then((module) => ({ default: module.PdfAttachmentPreview })));
const LazyLiveMarkdownEditor = lazy(() => import("./LiveMarkdownEditor").then((module) => ({ default: module.LiveMarkdownEditor })));

type MonacoEditorInstance = EditorTextSurface;

type EditorInsertEvent = CustomEvent<{ text: string; handled?: boolean }>;
type EditorTarget = Pick<EditorOpenRequest, "path" | "line" | "column" | "endLine" | "endColumn">;
type SaveResult = { path: string; status: "saved" | "conflict" | "failed" | "ignored" };

const guessLanguage = (path: string): string => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (["ts", "tsx", "mts", "cts"].includes(ext)) return "typescript";
  if (["js", "jsx", "mjs", "cjs"].includes(ext)) return "javascript";
  if (ext === "py") return "python";
  if (["cpp", "cc", "cxx", "hpp", "hh", "hxx"].includes(ext)) return "cpp";
  if (["c", "h"].includes(ext)) return "c";
  if (ext === "json" || ext === "jsonc") return "json";
  if (ext === "md" || ext === "mdx") return "markdown";
  if (["css", "scss", "less"].includes(ext)) return ext;
  if (ext === "html" || ext === "htm") return "html";
  if (["yml", "yaml"].includes(ext)) return "yaml";
  if (ext === "toml") return "toml";
  return "plaintext";
};

const editorLanguages = [
  ["plaintext", "纯文本"], ["cpp", "C++"], ["c", "C"],
  ["typescript", "TypeScript"], ["javascript", "JavaScript"], ["python", "Python"],
  ["html", "HTML"], ["css", "CSS"], ["scss", "SCSS"], ["less", "Less"],
  ["json", "JSON"], ["yaml", "YAML"],
] as const;
const editorLanguageName = (language: string) => editorLanguages.find(([id]) => id === language)?.[1] ?? language;

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

const useRawFileUrl = (path: string, workingDirectory: string, retryNonce = 0): string => {
  const version = useAppStore((state) => {
    for (let i = state.fileChanges.length - 1; i >= 0; i--) {
      const change = state.fileChanges[i];
      if (workspaceRootsEqual(change.workspaceRoot, workingDirectory)
        && editorPathsEqual(change.path, path, workingDirectory)) return change.sequence;
    }
    return 0;
  });
  return useMemo(() => withPreviewCacheBust(rawFileUrl(path, workingDirectory, version), retryNonce), [path, workingDirectory, version, retryNonce]);
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

interface FileSnapshot {
  content: string;
  contentHash?: string;
  sizeBytes?: number;
  readOnly?: boolean;
}

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
  const explorerId = useId();
  const editorExplorerOpen = useAppStore((s) => s.editorExplorerOpen);
  const setEditorExplorerOpen = useAppStore((s) => s.setEditorExplorerOpen);
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const codeTextScale = useAppStore((s) => s.codeTextScale);
  const reducedMotion = useAppStore((s) => s.reducedMotion);
  const predictionUsage = useAppStore((s) => s.inlineCompletionUsage);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const projectIndex = useWorkspaceModelIndex(workingDirectory);
  const editorOpenRequests = useAppStore((s) => s.editorOpenRequests);
  const fileChanges = useAppStore((s) => s.fileChanges);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const conversationId = useAppStore((s) => s.conversationId);
  const turnDiff = useAppStore((s) => (conversationId ? s.turnDiffs[conversationId] : undefined));
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
  const navigation = useMemo(() => new EditorNavigation(), [workingDirectory]);
  const navigating = useRef(false);
  const [, refreshNavigation] = useState(0);
  const [symbolMode, setSymbolMode] = useState<"file" | "project" | null>(null);
  const [predictionReady, setPredictionReady] = useState(false);
  useEffect(() => { setSymbolMode(null); }, [workingDirectory, activeTabPath]);
  const navigateHistory = (direction: number) => {
    const location = navigation.go(direction);
    if (!location) return;
    navigating.current = true;
    useAppStore.getState().openEditorFile(location.path, undefined, { ...location, exact: true });
    refreshNavigation((version) => version + 1);
  };
  const [editorSelection, setEditorSelection] = useState({ modelPath: "", hasText: false });
  const savingPathsRef = useRef(new Map<string, Promise<SaveResult>>());
  const [saveStatus, setSaveStatus] = useState<"idle" | "saved" | "error">("idle");
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; path: string; workspaceRoot: string } | null>(null);
  const tabListRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<MonacoEditorInstance | null>(null);
  const persistReadingPositionRef = useRef(() => {});
  useEffect(() => {
    const persist = () => persistReadingPositionRef.current();
    window.addEventListener("pagehide", persist);
    return () => window.removeEventListener("pagehide", persist);
  }, []);
  const monacoRef = useRef<typeof import("monaco-editor") | null>(null);
  const monacoOpenerRef = useRef<{ dispose: () => void } | null>(null);
  const monacoModelPaths = useRef(new Map<string, string>());
  const pendingModelRename = useRef<{
    original: import("monaco-editor").editor.ITextModel;
    renamed: import("monaco-editor").editor.ITextModel;
    viewState: unknown;
  } | null>(null);
  const preferences = useAppStore((state) => state.workbenchPreferences);
  const wordWrap = preferences.wordWrap;
  const minimap = preferences.minimap;
  const setWordWrap = (change: (value: boolean) => boolean) => useAppStore.getState().setWorkbenchPreferences({ wordWrap: change(wordWrap) });
  const setMinimap = (change: (value: boolean) => boolean) => useAppStore.getState().setWorkbenchPreferences({ minimap: change(minimap) });
  const markdownSessions = useRef(new Map<string, MarkdownEditorSession>());
  const [editorEpoch, setEditorEpoch] = useState(0);
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
  const language = activeTab?.language ?? guessLanguage(activeTabPath ?? "");
  const monacoTheme = miniCodeMonacoThemeName(resolvedTheme);
  const canRenderMarkdown = Boolean(activeTab && isMarkdownPath(activeTab.path) && !activeTab.loading && !activeTab.error && !activeTab.largeFile);
  const canAskAboutSelection = Boolean(activeTab && !activeTab.loading && !activeTab.error && !activeTab.largeFile
    && !isPreviewableMediaPath(activeTab.path));
  const showEditorToolbar = canRenderMarkdown || canAskAboutSelection;
  const activeDisplayPath = activeTab ? toWorkspaceDisplayPath(activeTab.path, workingDirectory) : "";
  const activePathSegments = activeDisplayPath.split("/");
  const activeMonacoUri = activeTab ? editorModelUri(activeTab.path, workingDirectory) : "";
  const activeModelPath = canRenderMarkdown ? `/${activeTab?.id}` : activeMonacoUri ? decodeURIComponent(new URL(activeMonacoUri).pathname) : "";
  const hasActiveSelection = canAskAboutSelection && editorSelection.hasText && editorSelection.modelPath === activeModelPath;
  const markdownPreviewComponents = useMemo(
    () => createMarkdownPreviewComponents(activeTab?.path ?? "", workingDirectory, markdownScopeId, markdownHeadingId),
    [activeTab?.path, workingDirectory, markdownScopeId, markdownHeadingId],
  );
  const resolveMarkdownUrl = useMemo(() => (url: string) => {
    if (url.startsWith("#")) return `#${markdownScopeId}-${markdownHeadingSlug(decodeMarkdownFragment(url.slice(1)))}`;
    const asset = resolveWorkspaceAsset(url, activeTab?.path ?? "", workingDirectory);
    const version = fileChanges.slice().reverse().find((change) => workspaceRootsEqual(change.workspaceRoot, workingDirectory)
      && editorPathsEqual(change.path, asset.path, workingDirectory))?.sequence ?? 0;
    return `${rawFileUrl(asset.path, workingDirectory, version)}${asset.fragment}`;
  }, [activeTab?.path, workingDirectory, markdownScopeId, fileChanges]);
  const showEditorTabs = chrome === "full";
  const supportedEditorActions: EditorAction[] = canRenderMarkdown
    ? ["find", "replace", "gotoLine"]
    : ["find", "replace", "gotoLine", "foldAll", "unfoldAll", ...(["typescript", "javascript", "json", "html", "css", "scss", "less", "python", "yaml", "c", "cpp"].includes(language) ? ["format" as const] : []), ...(["typescript", "javascript", "python", "c", "cpp"].includes(language) ? ["definition", "references", "rename"] as const : [])];
  const runEditorAction = (action: EditorAction) => {
    const ids: Record<EditorAction, string> = {
      find: "actions.find", replace: "editor.action.startFindReplaceAction", gotoLine: "editor.action.gotoLine",
      foldAll: "editor.foldAll", unfoldAll: "editor.unfoldAll", format: "editor.action.formatDocument",
      definition: "editor.action.revealDefinition", references: "editor.action.referenceSearch.trigger", rename: "editor.action.rename",
    };
    editorRef.current?.focus();
    if (canRenderMarkdown) void editorRef.current?.getAction?.(ids[action])?.run();
    else editorRef.current?.trigger!("minicode.editor-action", ids[action], {});
  };

  useLayoutEffect(() => {
    if (!showEditorTabs || tabs.length === 0) return;
    const tabList = tabListRef.current!;
    const revealActive = () => {
      tabList.querySelector<HTMLElement>(".editor-tab[data-active]")
        ?.scrollIntoView({ inline: "nearest", block: "nearest", behavior: "auto" });
    };
    revealActive();
    const observer = new ResizeObserver(revealActive);
    observer.observe(tabList);
    return () => observer.disconnect();
  }, [activeTabPath, tabs.length, showEditorTabs]);

  useLayoutEffect(() => {
    if (monacoRef.current) defineMiniCodeMonacoTheme(monacoRef.current, resolvedTheme);
  }, [resolvedTheme]);
  useEffect(() => () => monacoOpenerRef.current?.dispose(), []);
  useLayoutEffect(() => {
    for (const tab of tabs) {
      if (isMarkdownPath(tab.path) || isPreviewableMediaPath(tab.path) || !isEditablePath(tab.path)) continue;
      const key = `${normalizeWorkspaceRoot(workingDirectory)}:${tab.id}`;
      const nextPath = editorModelUri(tab.path, workingDirectory);
      const previousPath = monacoModelPaths.current.get(key);
      const monaco = monacoRef.current;
      if (monaco && previousPath && previousPath !== nextPath) {
        const original = monaco.editor.getModel(monaco.Uri.parse(previousPath));
        if (original) {
          const active = editorRef.current?.getModel() === original;
          const viewState = active ? editorRef.current?.saveViewState?.() : null;
          const renamed = renameMonacoModel(monaco, original, monaco.Uri.parse(nextPath));
          if (active) pendingModelRename.current = { original, renamed, viewState };
          else original.dispose();
        }
      }
      monacoModelPaths.current.set(key, nextPath);
    }
  }, [tabs, workingDirectory, editorEpoch]);
  const activeGitChange = useMemo(() => {
    if (!activeTabPath) return null;
    const files = [
      ...gitChanges.workingTree,
      ...gitChanges.staged,
    ];
    return files.find((file) => editorPathsEqual(file.path, activeTabPath, workingDirectory) && file.patch) ?? null;
  }, [activeTabPath, workingDirectory, gitChanges.workingTree, gitChanges.staged]);

  const agentEditReview = useAgentEditReview({
    conversationId,
    editorRef,
    path: activeTab?.path,
    content: activeTab?.content,
    readOnly: activeTab?.readOnly,
    turnDiff,
    workingDirectory,
    editorEpoch,
  });

  useEffect(() => {
    setSaveStatus("idle");
  }, [activeTabPath, workingDirectory]);

  useEffect(() => setCtxMenu(null), [workingDirectory]);

  useEffect(() => {
    if (saveStatus !== "saved") return;
    const timer = window.setTimeout(() => setSaveStatus("idle"), 1400);
    return () => window.clearTimeout(timer);
  }, [saveStatus]);

  // Consume open requests from other panels
  useEffect(() => {
    for (const request of editorOpenRequests) {
      if (openingRequestsRef.current.has(request)) continue;
      openingRequestsRef.current.add(request);
      void resolveUnqualifiedEditorPath(request.path, workingDirectory, request.exact).then((resolvedPath) => {
        const state = useAppStore.getState();
        if (!state.editorOpenRequests.includes(request)) return;
        const activate = state.activeEditorOpenRequestId === request.id;
        openEditorTab(resolvedPath, { activate: false, preview: request.preview });
        if (activate) {
          handleSetActive(resolvedPath, { ...request, path: resolvedPath });
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
    const warning = editorFileLimitReason(snapshot.content, snapshot.sizeBytes);
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
    if (!tab || tab.loading || tab.error || tab.largeFile) return false;
    const editor = editorRef.current;
    if (!editor) return false;
    const lineNumber = Math.max(1, Math.floor(target.line));
    const column = Math.max(1, Math.floor(target.column ?? 1));
    if (target.endLine) {
      const model = editor.getModel()!;
      const endLineNumber = Math.min(target.endLine, model.getLineCount());
      editor.setSelection?.({ startLineNumber: lineNumber, startColumn: column, endLineNumber,
        endColumn: target.endColumn ?? model.getLineMaxColumn(endLineNumber) });
    } else editor.setPosition?.({ lineNumber, column });
    editor.revealPositionInCenter?.({ lineNumber, column });
    editor.revealLineInCenter?.(lineNumber);
    editor.focus();
    const position = editor.getPosition();
    if (position) setCursor({ line: position.lineNumber, column: position.column });
    if (editorPathsEqual(pendingRevealRef.current?.path, target.path, workingDirectory)) {
      pendingRevealRef.current = null;
    }
    navigating.current = false;
    return true;
  };

  const handleSetActive = (path: string, target?: EditorTarget) => {
    persistReadingPositionRef.current();
    if (target?.line && !navigating.current) {
      const position = editorRef.current?.getPosition();
      if (activeTabPath && position) navigation.record({ path: activeTabPath, line: position.lineNumber, column: position.column });
      navigation.record({ path, line: target.line, column: target.column ?? 1 }, true);
      navigating.current = true;
      refreshNavigation((version) => version + 1);
    } else if (!target?.line) {
      navigating.current = false;
      pendingRevealRef.current = null;
    }
    setActiveTab(path);
    if (target?.line) {
      const column = target.column ?? 1;
      pendingRevealRef.current = { ...target, path, column };
      window.setTimeout(() => revealEditorTarget({ ...target, path, column }), 0);
    }
  };

  useEffect(() => {
    if (!pendingRevealRef.current) return;
    const id = window.setTimeout(() => {
      revealEditorTarget();
    }, 0);
    return () => window.clearTimeout(id);
  }, [activeTabPath, activeTab?.loading, activeTab?.error, activeTab?.largeFile]);

  const closeEditorPanel = () => {
    removePanel(editorSlotId);
  };

  const hideEditor = () => {
    if (chatSlot) focusPanel(chatSlot.id);
    else closeEditorPanel();
  };

  const saveTab = (tab: EditorTab, saveWorkspace: string, announce = false): Promise<SaveResult> => {
    const existing = savingPathsRef.current.get(tab.id);
    if (existing) return existing;
    const request = (async (): Promise<SaveResult> => {
      const savePath = tab.path;
      let saveContent = tab.content;
      const saveOriginal = tab.original;
      const expectedHash = tab.contentHash ?? "";
      const saveEpochKey = tab.id;
      if (workspaceRootsEqual(useAppStore.getState().workingDirectory, saveWorkspace)
        && editorPathsEqual(useAppStore.getState().activeTabPath, savePath, saveWorkspace)) setSaveStatus("idle");
      const saveEpoch = (loadEpochRef.current.get(saveEpochKey) ?? 0) + 1;
      loadEpochRef.current.set(saveEpochKey, saveEpoch);
      try {
        if (useAppStore.getState().workbenchPreferences.formatOnSave && monacoRef.current) {
          const monaco = monacoRef.current;
          const model = monaco.editor.getModel(monaco.Uri.parse(editorModelUri(savePath, saveWorkspace)));
          if (model) {
            await formatEditorModel(monaco, model);
            saveContent = model.getValue();
            useAppStore.getState().adoptEditorModelChanges([{ path: savePath, content: saveContent, original: saveOriginal, contentHash: expectedHash }], saveWorkspace);
          }
        }
        // Both single-file and project saves share the backend's guarded mutation
        // queue and acknowledge the exact submitted buffer, preserving newer edits.
        const result = await compareWriteWorkspaceFile(savePath, expectedHash, saveContent, saveWorkspace);
        const state = useAppStore.getState();
        const workspace = workspaceRootsEqual(state.workingDirectory, saveWorkspace)
          ? state : editorStateForWorkspace(saveWorkspace);
        const currentTab = workspace.editorTabs.find((candidate) => candidate.id === saveEpochKey);
        if (!currentTab || currentTab.original !== saveOriginal || (currentTab.contentHash ?? "") !== expectedHash
          || loadEpochRef.current.get(saveEpochKey) !== saveEpoch) return { path: currentTab?.path ?? savePath, status: "ignored" };
        const currentPath = currentTab.path;
        const saveIsVisible = workspaceRootsEqual(state.workingDirectory, saveWorkspace)
          && editorPathsEqual(state.activeTabPath, currentPath, saveWorkspace);
        if (result.ok) {
          markTabSaved(currentPath, saveContent, result.file.content_hash, result.file.size_bytes ?? result.file.size, saveWorkspace);
          if (currentTab.externalChanged && workspaceRootsEqual(state.workingDirectory, saveWorkspace)) {
            void reloadFileFromDisk(currentPath, { silent: true, preserveEdits: true });
          }
          if (saveIsVisible) {
            setSaveStatus("saved");
            if (announce) pushToast(`已保存 ${basename(currentPath)}`, "success", 1600);
          }
          return { path: currentPath, status: "saved" };
        }
        if (saveIsVisible) setSaveStatus("error");
        if (result.conflict) {
          markTabExternalChanged(currentPath, { workspaceRoot: saveWorkspace });
          if (announce && saveIsVisible) pushToast(`${basename(currentPath)} 已在磁盘上更改。为避免覆盖，已跳过保存。`, "warning", 4200);
          return { path: currentPath, status: "conflict" };
        }
        if (announce && saveIsVisible) pushToast(result.message || `保存失败：${basename(currentPath)}`, "error", 3500);
        return { path: currentPath, status: "failed" };
      } catch (error) {
        const current = useAppStore.getState();
        const workspace = workspaceRootsEqual(current.workingDirectory, saveWorkspace)
          ? current : editorStateForWorkspace(saveWorkspace);
        const currentTab = workspace.editorTabs.find((candidate) => candidate.id === saveEpochKey);
        if (workspaceRootsEqual(current.workingDirectory, saveWorkspace)
          && currentTab && editorPathsEqual(current.activeTabPath, currentTab.path, saveWorkspace)) {
          setSaveStatus("error");
          if (announce) pushToast(`保存失败：${errorMessage(error)}`, "error", 3500);
        }
        return { path: currentTab?.path ?? savePath, status: "failed" };
      } finally {
        savingPathsRef.current.delete(saveEpochKey);
      }
    })();
    savingPathsRef.current.set(tab.id, request);
    return request;
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
    await saveTab(activeTab, workingDirectory, true);
  };

  const saveAll = async () => {
    const snapshot = useAppStore.getState();
    const saveWorkspace = snapshot.workingDirectory;
    const dirtyTabs = snapshot.editorTabs.filter((tab) => !tab.readOnly && !tab.loading && !tab.error && !tab.largeFile && tab.content !== tab.original);
    if (!dirtyTabs.length) {
      pushToast("所有文件均已保存。", "info", 1400);
      return;
    }
    const results: SaveResult[] = [];
    for (const requestedTab of dirtyTabs) {
      const pending = savingPathsRef.current.get(requestedTab.id);
      const settled = pending ? await pending : undefined;
      if (settled && settled.status !== "saved") {
        results.push(settled);
        continue;
      }
      const current = useAppStore.getState();
      const workspace = workspaceRootsEqual(current.workingDirectory, saveWorkspace)
        ? current : editorStateForWorkspace(saveWorkspace);
      // A preceding request may have awaited a rename or workspace switch. Save
      // the same buffer at its current path within the original workspace.
      const tab = workspace.editorTabs.find((candidate) => candidate.id === requestedTab.id);
      if (tab && tab.content !== tab.original) results.push(await saveTab(tab, saveWorkspace));
      else if (settled) results.push(settled);
    }
    const saved = results.filter((result) => result.status === "saved").length;
    const conflicts = results.filter((result) => result.status === "conflict").map((result) => result.path);
    const failures = results.filter((result) => result.status === "failed").map((result) => result.path);
    const scope = workspaceRootsEqual(useAppStore.getState().workingDirectory, saveWorkspace) ? "" : `${saveWorkspace}：`;
    const summary = [
      `${scope}已保存 ${saved} 个文件`,
      conflicts.length ? `磁盘冲突：${conflicts.join("、")}` : "",
      failures.length ? `保存失败：${failures.join("、")}` : "",
    ].filter(Boolean).join("；");
    pushToast(summary, failures.length ? "error" : conflicts.length ? "warning" : saved ? "success" : "info", failures.length || conflicts.length ? 6000 : 1800);
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
    const newChanges = fileChanges.filter((change) => change.sequence > lastFileChangeSequence.current
      && workspaceRootsEqual(change.workspaceRoot, workingDirectory));
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

  const askAboutSelection = (editor: MonacoEditorInstance) => {
    const selection = editor.getSelection();
    const text = selection ? editor.getModel()!.getValueInRange(selection) : "";
    if (!text.trim()) {
      pushToast("请先选择一些代码。", "warning");
      return;
    }
    const state = useAppStore.getState();
    state.openSideChatWithSelection(text, state.activeTabPath ?? undefined, {
      range: {
        startLineNumber: selection!.startLineNumber, startColumn: selection!.startColumn,
        endLineNumber: selection!.endLineNumber, endColumn: selection!.endColumn,
      },
      workspaceRoot: state.workingDirectory,
    });
  };

  const mountTextSurface = (editor: EditorTextSurface) => {
    const mountedEditor = editor;
    if (!isMarkdownPath(activeTab!.path)) {
      const native = editor as unknown as import("monaco-editor").editor.IStandaloneCodeEditor;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "mc-editor-selection-trigger";
      button.textContent = "提问 · Ctrl+K";
      button.onclick = () => askAboutSelection(mountedEditor);
      const widget = {
        getId: () => "minicode.selection.question",
        getDomNode: () => button,
        getPosition: () => { const selection = native.getSelection(); return selection && !selection.isEmpty() ? { position: selection.getEndPosition(), preference: [2, 1] } : null; },
      };
      native.addContentWidget(widget);
      const selectionChanged = native.onDidChangeCursorSelection(() => native.layoutContentWidget(widget));
      native.onDidDispose(() => { selectionChanged.dispose(); native.removeContentWidget(widget); });
    }
    const mountedWorkspace = workingDirectory;
    let modelPath = activeTab!.path;
    let releaseSurface = registerWorkspaceEditorSurface(mountedWorkspace, modelPath, mountedEditor);
    let modelResource = mountedEditor.getModel()?.uri.path;
    let restoringReading = false;
    const persistReadingPosition = () => {
      if (restoringReading || mountedEditor.getModel()?.uri.path !== modelResource) return;
      const reading = mountedEditor.saveViewState?.();
      if (reading) persistEditorViewState(mountedWorkspace, modelPath, { kind: isMarkdownPath(modelPath) ? "markdown" : "code", state: reading });
    };
    const restoreReadingPosition = () => {
      const state = useAppStore.getState();
      const workspace = workspaceRootsEqual(state.workingDirectory, mountedWorkspace) ? state : editorStateForWorkspace(mountedWorkspace);
      const resource = mountedEditor.getModel()?.uri.path;
      if (!resource) return;
      const filePath = resource.replace(/^\/(?=[A-Za-z]:\/)/, "");
      const tab = workspace.editorTabs.find((entry) => isMarkdownPath(entry.path)
        ? resource === `/${entry.id}` : editorPathsEqual(filePath, entry.path, mountedWorkspace));
      if (!tab) return;
      modelPath = tab.path;
      releaseSurface();
      releaseSurface = registerWorkspaceEditorSurface(mountedWorkspace, modelPath, mountedEditor);
      modelResource = resource;
      const reading = loadEditorViewState(mountedWorkspace, modelPath);
      // The wrapper restores in-memory state after setModel. Apply the
      // persisted state afterwards, before the existing source-link reveal.
      restoringReading = true;
      queueMicrotask(() => {
        if (editorRef.current === mountedEditor && mountedEditor.getModel()?.uri.path === resource && reading?.kind === (isMarkdownPath(modelPath) ? "markdown" : "code")) mountedEditor.restoreViewState?.(reading.state);
        restoringReading = false;
      });
    };
    persistReadingPositionRef.current = persistReadingPosition;
    editorRef.current = mountedEditor;
    setEditorEpoch((epoch) => epoch + 1);
    editor.onDidDispose(() => {
      releaseSurface();
      persistReadingPosition();
      if (editorRef.current === editor) {
        editorRef.current = null;
        setEditorSelection({ modelPath: "", hasText: false });
      }
    });
    const syncSelection = () => {
      const model = mountedEditor.getModel();
      const selection = mountedEditor.getSelection();
      setEditorSelection({
        modelPath: model?.uri.path ?? "",
        hasText: Boolean(model && selection && model.getValueInRange(selection).trim()),
      });
      const position = mountedEditor.getPosition();
      if (position) {
        setCursor({ line: position.lineNumber, column: position.column });
        if (!restoringReading && !navigating.current && modelResource === model?.uri.path) {
          navigation.record({ path: modelPath, line: position.lineNumber, column: position.column });
          refreshNavigation((version) => version + 1);
        }
        agentEditReview.onCursorLine(position.lineNumber);
      }
    };
    mountedEditor.onDidChangeCursorSelection(() => { syncSelection(); persistReadingPosition(); });
    mountedEditor.onDidScrollChange?.(persistReadingPosition);
    mountedEditor.onDidChangeModel(() => {
      const pending = pendingModelRename.current;
      if (pending && mountedEditor.getModel() === pending.renamed) {
        pendingModelRename.current = null;
        // The React Monaco wrapper restores path-keyed state after setModel.
        // Apply the renamed buffer's state after that wrapper finishes.
        queueMicrotask(() => {
          mountedEditor.restoreViewState?.(pending.viewState);
          pending.original.dispose();
        });
      }
      syncSelection();
      restoreReadingPosition();
      // Monaco restores the incoming model's saved position after setModel.
      queueMicrotask(() => {
        if (editorRef.current === mountedEditor) syncSelection();
      });
    });
    mountedEditor.onDidChangeModelContent(syncSelection);
    restoreReadingPosition();
    syncSelection();
    mountedEditor.addAction?.({
      id: "minicode.ask-about-selection",
      label: "在侧边对话中询问所选内容",
      contextMenuGroupId: "navigation",
      contextMenuOrder: 1.5,
      run: askAboutSelection,
    });
    editor.onDidChangeCursorPosition((event) => {
      setCursor({ line: event.position.lineNumber, column: event.position.column });
      agentEditReview.onCursorLine(event.position.lineNumber);
    });
    window.setTimeout(() => { revealEditorTarget(); syncSelection(); }, 0);
  };

  useEffect(() => {
    const handleInsert = (event: Event) => {
      const detail = (event as EditorInsertEvent).detail;
      if (!detail?.text || !activeTab || activeTab.loading || activeTab.error || activeTab.largeFile || activeTab.readOnly) return;
      const editor = editorRef.current;
      if (!editor) return;
      const selection = editor.getSelection();
      if (!selection) return;
      editor.pushUndoStop?.();
      editor.executeEdits("chat-code-insert", [{
        range: selection,
        text: detail.text,
        forceMoveMarkers: true,
      }]);
      editor.pushUndoStop?.();
      editor.focus();
      detail.handled = true;
    };
    window.addEventListener("editor:insert-text", handleInsert);
    return () => window.removeEventListener("editor:insert-text", handleInsert);
  }, [activeTab]);

  // Keyboard shortcut listeners (Ctrl+S, Ctrl+W)
  const saveRef = useRef(save);
  const saveAllRef = useRef(saveAll);
  const closeTabRef = useRef(handleCloseTab);
  saveRef.current = save;
  saveAllRef.current = saveAll;
  closeTabRef.current = handleCloseTab;
  useEffect(() => {
    const handleSave = () => void saveRef.current();
    const handleSaveAll = () => void saveAllRef.current();
    const handleCloseTabEvent = () => {
      if (activeTabPath) closeTabRef.current(activeTabPath);
    };
    window.addEventListener("editor:save", handleSave);
    window.addEventListener("editor:save-all", handleSaveAll);
    window.addEventListener("editor:close-tab", handleCloseTabEvent);
    return () => {
      window.removeEventListener("editor:save", handleSave);
      window.removeEventListener("editor:save-all", handleSaveAll);
      window.removeEventListener("editor:close-tab", handleCloseTabEvent);
    };
  }, [activeTabPath]);

  return (
    <div className="mc-editor-panel flex-1 min-h-0 flex flex-col">
      <div className="mc-editor-workspace">
        <aside id={explorerId} className="mc-editor-explorer" aria-label="项目文件" style={{ display: editorExplorerOpen ? "flex" : "none" }}>
          <FileTree />
        </aside>
        <div className="mc-editor-document" onKeyDownCapture={(event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && hasActiveSelection) {
        event.preventDefault(); event.stopPropagation(); askAboutSelection(editorRef.current!);
      } else if (event.altKey && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
        event.preventDefault(); event.stopPropagation(); navigateHistory(event.key === "ArrowLeft" ? -1 : 1);
      } else if ((event.ctrlKey || event.metaKey) && (event.key.toLowerCase() === "t" || (event.shiftKey && event.key.toLowerCase() === "o")) && !canRenderMarkdown) {
        event.preventDefault(); event.stopPropagation(); setSymbolMode(event.key.toLowerCase() === "t" ? "project" : "file");
      } else if (event.altKey && !event.ctrlKey && !event.metaKey && event.code === "KeyZ") {
        event.preventDefault();
        event.stopPropagation();
        setWordWrap((value) => !value);
      }
        }}>
        <div className="mc-editor-tabbar">
          <button
            type="button"
            className="mc-editor-explorer-toggle"
            aria-label={editorExplorerOpen ? "收起项目文件" : "展开项目文件"}
            title={editorExplorerOpen ? "收起项目文件" : "展开项目文件"}
            aria-expanded={editorExplorerOpen}
            aria-controls={explorerId}
            onClick={() => setEditorExplorerOpen(!editorExplorerOpen)}
          ><FolderTree size={16} aria-hidden="true" /></button>
      {showEditorTabs && tabs.length > 0 && (
        <div ref={tabListRef} role="tablist" aria-label="打开的文件" className="mc-editor-tabs">
          {tabs.map((tab, tabIndex) => {
            const tabDirty = tab.content !== tab.original;
            const active = editorPathsEqual(tab.path, activeTabPath, workingDirectory);
            return (
              <div
                key={tab.id}
                role="presentation"
                className="editor-tab"
                data-active={active || undefined}
                data-preview={tab.preview || undefined}
                data-pinned={tab.pinned || undefined}
                onDoubleClick={() => useAppStore.getState().keepEditorTab(tab.path)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  setCtxMenu({ x: e.clientX, y: e.clientY, path: tab.path, workspaceRoot: workingDirectory });
                }}
                onAuxClick={(event) => {
                  if (event.button !== 1) return;
                  event.preventDefault();
                  void handleCloseTab(tab.path);
                }}
                title={tab.path}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={active}
                  aria-label={basename(tab.path)}
                  aria-description={tabDirty ? `${tab.path}，未保存` : tab.path}
                  tabIndex={active ? 0 : -1}
                  onClick={() => handleSetActive(tab.path)}
                  onKeyDown={(event) => {
                    const nextIndex = event.key === "ArrowRight" ? (tabIndex + 1) % tabs.length
                      : event.key === "ArrowLeft" ? (tabIndex - 1 + tabs.length) % tabs.length
                      : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
                    if (nextIndex < 0) return;
                    event.preventDefault();
                    handleSetActive(tabs[nextIndex].path);
                    tabListRef.current!.querySelectorAll<HTMLButtonElement>('[role="tab"]')[nextIndex].focus();
                  }}
                  className="min-w-0 flex-1 inline-flex items-center gap-1.5 border-0 bg-transparent p-0 cursor-pointer"
                  style={{ color: "inherit", fontFamily: "inherit" }}
                  title={tab.path}
                >
                  <span className="editor-tab-file-icon shrink-0" style={{ color: fileGlyphColor(tab.path) }} aria-hidden="true">
                    {fileIcon(tab.path, { size: 16, className: "editor-tab-file-icon-svg" })}
                  </span>
                <span className="overflow-hidden text-ellipsis whitespace-nowrap text-xs">
                  {tab.pinned ? "· " : ""}{basename(tab.path)}
                </span>
                </button>
                <button
                  type="button"
                  className="editor-tab-close"
                  data-dirty={tabDirty || undefined}
                  tabIndex={active ? 0 : -1}
                  title={tabDirty ? "未保存 · 关闭标签页" : "关闭标签页"}
                  aria-label={`关闭 ${basename(tab.path)}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    handleCloseTab(tab.path);
                  }}
                >
                  {tabDirty && <Circle className="editor-tab-dirty" size={9} fill="currentColor" aria-hidden="true" />}
                  <X className="editor-tab-dismiss" size={14} aria-hidden="true" />
                </button>
              </div>
            );
          })}
        </div>
      )}
        </div>
      {showEditorToolbar && (
        <div className="mc-editor-toolbar">
          <div className="mc-editor-actions" role="toolbar" aria-label="代码导航">
            <button type="button" aria-label="返回上一位置" title="返回 · Alt+←" disabled={navigation.index <= 0} onClick={() => navigateHistory(-1)}><ArrowLeft size={14} /></button>
            <button type="button" aria-label="前进到下一位置" title="前进 · Alt+→" disabled={navigation.index >= navigation.entries.length - 1} onClick={() => navigateHistory(1)}><ArrowRight size={14} /></button>
          </div>
          <nav aria-label="文件路径" title={activeDisplayPath} className="mc-editor-breadcrumbs">
            <ol className="flex items-center gap-1 min-w-0 overflow-hidden">
              {activePathSegments.map((segment, index) => (
                <li key={index} className="inline-flex items-center gap-1 min-w-0" style={{ color: index === activePathSegments.length - 1 ? "var(--text-secondary)" : undefined }}>
                  {index > 0 && <ChevronRight size={12} className="shrink-0" aria-hidden="true" />}
                  <span className="overflow-hidden text-ellipsis whitespace-nowrap">{segment}</span>
                </li>
              ))}
            </ol>
          </nav>
          {hasActiveSelection && (
            <button
              type="button"
              onClick={() => askAboutSelection(editorRef.current!)}
              title="将当前所选代码带入侧边对话 · Ctrl+K"
              className="mc-editor-selection-action"
            >
              <MessageSquare size={14} />
              提问 · Ctrl+K
            </button>
          )}
          <EditorActions
            onAction={runEditorAction}
            onSaveAll={() => void saveAll()}
            dirtyCount={tabs.filter((tab) => !tab.readOnly && !tab.loading && !tab.error && !tab.largeFile && tab.content !== tab.original).length}
            supportedActions={supportedEditorActions}
            wordWrap={wordWrap}
            onToggleWordWrap={() => setWordWrap((value) => !value)}
            minimap={minimap}
            onToggleMinimap={() => setMinimap((value) => !value)}
            showMinimap={!canRenderMarkdown}
            readOnly={Boolean(activeTab?.readOnly)}
          />
          {!canRenderMarkdown && <button type="button" className="mc-editor-symbol-button" aria-label="文件大纲" title="文件大纲 · Ctrl+Shift+O（Shift 点击：项目符号）" onClick={(event) => setSymbolMode(event.shiftKey ? "project" : "file")}><Braces size={15} /></button>}
        </div>
      )}

      {symbolMode && monacoRef.current && <Suspense fallback={null}><LazyEditorSymbols monaco={monacoRef.current} workspaceRoot={workingDirectory} path={activeMonacoUri} project={symbolMode === "project"} onClose={() => setSymbolMode(null)} /></Suspense>}
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
            <FileLoadErrorNotice path={activeTab.path} error={activeTab.error} onClose={() => closeEditorTab(activeTab.path)} onRetry={() => {
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
          ) : canRenderMarkdown ? (
            <div className="agent-edit-editor-host relative flex-1 min-h-0 flex flex-col">
              <Suspense fallback={<EditorLoading />}>
                <LazyLiveMarkdownEditor
                  documentId={activeTab.id}
                  sessions={markdownSessions.current}
                  value={activeTab.content}
                  readOnly={Boolean(activeTab.readOnly)}
                  textScale={codeTextScale}
                  wordWrap={wordWrap}
                  scopeId={markdownScopeId}
                  components={markdownPreviewComponents}
                  urlTransform={markdownUrlTransform}
                  resolveUrl={resolveMarkdownUrl}
                  onChange={(value) => updateTabContent(activeTab.path, value)}
                  onMount={mountTextSurface}
                  bufferTransactions={activeTab.pendingBufferTransactions}
                  onConsumeBufferTransactions={() => useAppStore.getState().consumeEditorBufferTransactions(activeTab.path, workingDirectory)}
                />
              </Suspense>
              <AgentEditReviewBar
                total={agentEditReview.total}
                currentIndex={agentEditReview.currentIndex}
                currentLine={agentEditReview.currentLine}
                onReveal={agentEditReview.reveal}
                onPrev={agentEditReview.prev}
                onNext={agentEditReview.next}
                onKeep={agentEditReview.keep}
                onUndo={agentEditReview.undo}
                onKeepAll={agentEditReview.keepAll}
              />
            </div>
          ) : (
            <div className="agent-edit-editor-host relative flex-1 min-h-0 flex flex-col">
            <Suspense fallback={<EditorLoading />}>
              <LazyMonacoEditor
                key={normalizeWorkspaceRoot(workingDirectory)}
                height="100%"
                language={language}
                theme={monacoTheme}
                beforeMount={(monaco) => {
                  monacoRef.current = monaco;
                  projectIndex.initialize(monaco);
                  defineMiniCodeMonacoTheme(monaco, useAppStore.getState().resolvedTheme);
                  monacoOpenerRef.current?.dispose();
                  monacoOpenerRef.current = registerMiniCodeEditorOpener(monaco, (path, label, target) => useAppStore.getState().openEditorFile(path, label, target));
                }}
                path={activeMonacoUri}
                keepCurrentModel={!activeTab.readOnly && (projectIndex.retainsModel(activeTab.path) || retainsWorkspaceBufferEditModel(workingDirectory, activeTab.path))}
                loading={<EditorLoading />}
                value={activeTab.content}
                onChange={(value) => {
                  if (!projectIndex.ownsModel(activeTab.path)) updateTabContent(activeTab.path, value ?? "");
                }}
                onMount={(editor) => {
                  editor.getContribution("editor.contrib.inlineCompletionsController");
                  mountTextSurface(editor as MonacoEditorInstance);
                  setPredictionReady(true);
                  editor.onDidDispose(() => setPredictionReady(false));
                  editor.addAction({ id: "minicode.predict", label: "代码预测",
                    keybindings: [monacoRef.current!.KeyMod.Alt | monacoRef.current!.KeyCode.Backslash],
                    run: (target) => {
                      const store = useAppStore.getState();
                      if (!store.workbenchPreferences.aiEnabled) store.setWorkbenchPreferences({ aiEnabled: true });
                      requestAnimationFrame(() => {
                        target.trigger("minicode", "hideSuggestWidget", null);
                        target.trigger("minicode", "editor.action.inlineSuggest.trigger", null);
                      });
                    },
                  });
                }}
                options={{
                  ...miniCodeCodeEditingOptions,
                  inlineSuggest: { enabled: preferences.aiEnabled, showToolbar: "onHover" },
                  automaticLayout: true,
                  readOnly: Boolean(activeTab.readOnly),
                  fontFamily: "var(--editor-font-family)",
                  fontSize: Math.round(14 * codeTextScale),
                  lineHeight: Math.round(22 * codeTextScale),
                  minimap: { enabled: minimap },
                  scrollBeyondLastLine: false,
                  wordWrap: wordWrap ? "on" : "off",
                  fontLigatures: false,
                  tabSize: preferences.tabSize,
                  insertSpaces: preferences.insertSpaces,
                  detectIndentation: false,
                  lineNumbers: preferences.lineNumbers ? "on" : "off",
                  cursorBlinking: reducedMotion ? "solid" : "smooth",
                  cursorSmoothCaretAnimation: reducedMotion ? "off" : "on",
                  renderLineHighlight: "all",
                  renderWhitespace: "selection",
                  roundedSelection: false,
                  padding: { top: 12, bottom: 20 },
                  lineNumbersMinChars: 4,
                  lineDecorationsWidth: 12,
                  renderFinalNewline: "dimmed",
                  folding: true,
                  glyphMargin: agentEditReview.total > 0,
                  bracketPairColorization: { enabled: true },
                  guides: { indentation: true, bracketPairs: true },
                  smoothScrolling: !reducedMotion,
                  stickyScroll: { enabled: preferences.stickyScroll },
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
            <AgentEditReviewBar
              total={agentEditReview.total}
              currentIndex={agentEditReview.currentIndex}
              currentLine={agentEditReview.currentLine}
              onReveal={agentEditReview.reveal}
              onPrev={agentEditReview.prev}
              onNext={agentEditReview.next}
              onKeep={agentEditReview.keep}
              onUndo={agentEditReview.undo}
              onKeepAll={agentEditReview.keepAll}
            />
            </div>
          )
        ) : (
          <div className="h-full flex flex-col items-center justify-center gap-2.5 text-sm" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
            <span className="editor-empty-file-icon" aria-hidden="true"><FileCode2 size={28} strokeWidth={1.8} className="editor-empty-file-icon-svg" /></span>
            <div className="font-semibold" style={{ color: "var(--text-secondary)" }}>未打开文件</div>
            <div className="max-w-[420px] text-center">
              从项目文件中选择文件，或按 Ctrl+P 快速打开。
            </div>
          </div>
        )}
      </div>

      <div title={activeTabPath ?? ""} className="mc-editor-status">
        <span className="mc-editor-status-file" data-dirty={dirty || undefined}>
          {activeTabPath ? basename(activeTabPath) : "未打开文件"}{dirty ? " · 已修改" : ""}
        </span>
        {activeTab?.sizeBytes != null && <span className="mc-editor-status-size">{formatBytes(activeTab.sizeBytes)}</span>}
        {activeTab?.draftRestored && <span title={activeTab.loadWarning || "未保存正文已从本机草稿恢复"}>已恢复草稿</span>}
        {activeTab?.readOnly && (
          <span className="inline-flex items-center gap-1" title="MiniCode 生成的只读工具结果">
            <LockKeyhole size={14} /> 只读
          </span>
        )}
        {activeTab && canAskAboutSelection && !canRenderMarkdown ? (
          <SelectMenu
            className="mc-editor-language-select"
            ariaLabel="文件语言"
            title="更改文件语言模式；纯文本不会显示语法颜色"
            align="end"
            value={activeTab.language ?? "auto"}
            onValueChange={(value) => useAppStore.getState().setEditorTabLanguage(activeTab.path, value === "auto" ? undefined : value)}
          >
            <option value="auto">自动 · {editorLanguageName(guessLanguage(activeTab.path))}</option>
            {editorLanguages.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </SelectMenu>
        ) : <span>{editorLanguageName(language)}</span>}
        {activeTab && !canRenderMarkdown && !isImagePath(activeTab.path) && !isPdfPath(activeTab.path) && !activeTab.readOnly && !activeTab.largeFile && <button
          type="button" className="mc-editor-prediction-status" data-error={Boolean(predictionUsage.lastError)}
          aria-label={preferences.aiEnabled ? "触发代码预测" : "开启代码预测"}
          title={predictionUsage.lastError || "暂停输入自动预测。Alt + \\ 手动触发，Tab 接受，Esc 取消。"}
          disabled={!predictionReady || predictionUsage.pending}
          onClick={() => {
            const target = editorRef.current as unknown as import("monaco-editor").editor.IStandaloneCodeEditor;
            target.focus();
            void target.getAction("minicode.predict")!.run();
          }}><Sparkles size={12} /><span>{predictionUsage.pending ? "正在预测…" : predictionUsage.lastError ? "预测失败 · 重试" : preferences.aiEnabled ? "代码预测" : "开启预测"}</span></button>}
        {activeGitChange?.patch && (
          <button
            type="button"
            onClick={openActiveFileDiff}
            className="mc-editor-status-diff"
          >
            <GitCompare size={14} />
            Diff
            <span style={{ color: "var(--diff-added-foreground)" }}>+{activeGitChange.additions}</span>
            <span style={{ color: "var(--diff-removed-foreground)" }}>-{activeGitChange.deletions}</span>
          </button>
        )}
        {saveStatus === "saved" && <span style={{ color: "var(--state-success)" }}>已保存</span>}
        {saveStatus === "error" && <span style={{ color: "var(--state-danger)" }}>保存失败</span>}
        {showEditorToolbar && projectIndex.status.phase !== "idle" && <button
          type="button"
          className="mc-editor-index-status"
          data-phase={projectIndex.status.phase}
          onClick={projectIndex.refresh}
          disabled={projectIndex.status.phase === "loading"}
          title={projectIndex.status.issues.length
            ? projectIndex.status.issues.map((issue) => `${issue.path}: ${issue.message}`).join("\n")
            : "TS/JS 项目索引，编辑缓冲区优先；点击重新索引项目与依赖类型"}
        >{projectIndex.status.phase === "loading" ? "正在索引…"
          : projectIndex.status.phase === "error" ? "索引失败 · 重试"
          : projectIndex.status.phase === "partial" ? `索引有缺项 · ${projectIndex.status.issues.length}`
          : `项目索引 · ${projectIndex.status.sourceCount} 个源码`}</button>}
        {showEditorToolbar && <span>{`第 ${cursor.line} 行，第 ${cursor.column} 列`}</span>}
      </div>
        </div>
      </div>

      {ctxMenu && workspaceRootsEqual(ctxMenu.workspaceRoot, workingDirectory) && (
        <ContextMenu
          position={{ x: ctxMenu.x, y: ctxMenu.y }}
          onClose={() => setCtxMenu(null)}
          items={[
            { label: tabs.find((tab) => tab.path === ctxMenu.path)?.pinned ? "取消固定标签" : "固定标签",
              onClick: () => useAppStore.getState().pinEditorTab(ctxMenu.path, !tabs.find((tab) => tab.path === ctxMenu.path)?.pinned) },
            { label: "保留标签", onClick: () => useAppStore.getState().keepEditorTab(ctxMenu.path) },
            { label: "关闭标签页", onClick: () => handleCloseTab(ctxMenu.path) },
            {
              label: "关闭其他标签页",
              onClick: () => {
                const paths = tabs.filter((tab) => !tab.pinned && !editorPathsEqual(tab.path, ctxMenu.path, workingDirectory)).map((tab) => tab.path);
                void handleCloseTabs(paths);
              },
            },
            { label: "关闭所有标签页", onClick: () => { void handleCloseTabs(tabs.map((tab) => tab.path)); } },
            {
              label: "关闭右侧标签页",
              onClick: () => {
                const idx = tabs.findIndex((t) => editorPathsEqual(t.path, ctxMenu.path, workingDirectory));
                void handleCloseTabs(tabs.slice(idx + 1).filter((tab) => !tab.pinned).map((tab) => tab.path));
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

const FileLoadErrorNotice = ({ path, error, onRetry, onClose }: { path: string; error: string; onRetry: () => void; onClose: () => void }) => {
  const missing = /ENOENT|no such file or directory|not found/i.test(error);
  return (
    <div className="h-full flex flex-col items-center justify-center gap-[9px] p-6 text-center" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
      <FileWarning size={28} style={{ color: "var(--state-warning)" }} />
      <div className="font-bold" style={{ color: "var(--text-primary)" }}>{missing ? "文件不存在或已移动" : "无法加载文件"}</div>
      <div className="max-w-[560px] leading-[1.5]" style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)" }}>
        {missing ? "当前路径没有这个文件。请检查文件位置，或关闭这个标签。" : "读取失败。请重试，或查看技术详情。"}
      </div>
      <div className="max-w-[560px] overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
        {path}
      </div>
      <div className="flex gap-2">
        <button type="button" onClick={onRetry} className="inline-flex items-center h-[30px] px-2.5 border rounded-[4px] cursor-pointer font-semibold" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-raised)", color: "var(--text-primary)", fontSize: "var(--text-xs)" }}>重试</button>
        <button type="button" onClick={onClose} className="inline-flex items-center h-[30px] px-2.5 border rounded-[4px] cursor-pointer" style={{ borderColor: "var(--border-subtle)", background: "transparent", color: "var(--text-secondary)", fontSize: "var(--text-xs)" }}>关闭标签</button>
      </div>
      <details className="max-w-[560px] text-left" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
        <summary className="cursor-pointer">技术详情</summary>
        <div className="mt-2 break-all" style={{ fontFamily: "var(--font-mono)" }}>{error}</div>
      </details>
    </div>
  );
};

const ImageViewer = ({ path, workingDirectory }: { path: string; workingDirectory: string }) => {
  const [retryNonce, setRetryNonce] = useState(0);
  const imgSrc = useRawFileUrl(path, workingDirectory, retryNonce);
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [imgSrc]);

  return (
    <div className="flex-1 min-h-0 flex items-center justify-center p-6 overflow-auto" style={{ background: "var(--surface-base)" }}>
      {failed ? (
        <div className="flex flex-col items-center gap-2" style={{ color: "var(--text-muted)" }}>
          <Image size={32} />
          <span style={{ fontSize: "var(--text-sm)" }}>无法显示图片</span>
          <span style={{ fontSize: "var(--text-sm)", fontFamily: "var(--font-ui)" }}>{basename(path)}</span>
          <button type="button" className="btn-secondary" onClick={() => setRetryNonce((value) => value + 1)}>重试图片预览</button>
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
  const [retryNonce, setRetryNonce] = useState(0);
  const src = useRawFileUrl(path, workingDirectory, retryNonce);
  return (
    <Suspense fallback={<EditorLoading />}>
      <LazyPdfPreview url={src} name={basename(path)} onRetry={() => setRetryNonce((value) => value + 1)} />
    </Suspense>
  );
};

// TabContextMenu used to be defined inline here; now replaced by the
// generic ContextMenu component in ../components/ContextMenu.
