import { useEffect, useMemo, useRef, useState } from "react";
import { lazy, Suspense } from "react";
import { Circle, Edit3, Eye, FileWarning, GitCompare, Image, X } from "lucide-react";
import { fileGlyphColor, fileIcon } from "../shell/fileTreeHelpers";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import { useAppStore } from "../stores";
import { workspaceRawResourceUrlWithToken } from "../protocol/api";
import {
  compareWriteWorkspaceFile,
  readWorkspaceFile,
} from "../protocol/workspace";
import { fsCompareWriteFile, fsReadFileInfo, isDesktop, revealPath } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";

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
    import("monaco-editor/esm/vs/editor/editor.api.js"),
    import("monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution.js"),
    import("monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution.js"),
    import("monaco-editor/esm/vs/basic-languages/css/css.contribution.js"),
    import("monaco-editor/esm/vs/basic-languages/html/html.contribution.js"),
    import("monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js"),
    import("monaco-editor/esm/vs/basic-languages/python/python.contribution.js"),
  ]);
  reactMonaco.loader?.config?.({ monaco });
  return { default: reactMonaco.default };
});

type MonacoEditorInstance = {
  getSelection: () => unknown;
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

const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "gif", "webp", "ico", "bmp", "svg"]);
const PDF_EXTENSIONS = new Set(["pdf"]);

const isImagePath = (path: string): boolean => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_EXTENSIONS.has(ext);
};

const isPdfPath = (path: string): boolean => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return PDF_EXTENSIONS.has(ext);
};

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
  const normalized = decodedSeparators.replace(/\\/g, "/").replace(/\/+/g, "/");
  const root = workingDirectory.trim().replace(/\\/g, "/").replace(/\/+$/, "");
  if (!normalized || !root) return normalized;
  const normalizedLower = normalized.toLowerCase();
  const rootLower = root.toLowerCase();
  if (normalizedLower === rootLower) return ".";
  if (normalizedLower.startsWith(`${rootLower}/`)) {
    return normalized.slice(root.length).replace(/^\/+/, "") || ".";
  }
  return normalized;
};

const rawFileUrl = (path: string, workingDirectory = ""): string => {
  const normalized = toWorkspaceDisplayPath(path, workingDirectory);
  if (/^(https?:|data:|blob:|file:|mailto:|tel:|#)/i.test(normalized)) return normalized;
  return workspaceRawResourceUrlWithToken(normalized);
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
  return left === right || left.endsWith(`/${right}`) || right.endsWith(`/${left}`);
};

interface FileSnapshot {
  content: string;
  contentHash?: string;
  sizeBytes?: number;
}

const MAX_EDITOR_BYTES = 2 * 1024 * 1024;
const MAX_EDITOR_CHARS = 1_000_000;
const MAX_EDITOR_LINES = 20_000;
const MAX_MARKDOWN_PREVIEW_IMAGES = 80;
const EDITOR_FRAME_REFERRER_POLICY = "no-referrer";

const formatBytes = (bytes?: number): string => {
  if (!bytes || !Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

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
    return `This file is ${formatBytes(bytes)}, which is above the ${formatBytes(MAX_EDITOR_BYTES)} editor limit.`;
  }
  if (snapshot.content.length > MAX_EDITOR_CHARS) {
    return `This file has ${snapshot.content.length.toLocaleString()} characters, which is above the editor limit.`;
  }
  const lines = countLines(snapshot.content);
  if (lines > MAX_EDITOR_LINES) {
    return `This file has ${lines.toLocaleString()} lines, which is above the editor limit.`;
  }
  return null;
};

const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error || "Could not read file.");

const isLargeFileError = (message: string): boolean =>
  /too large|max supported size|above the .*limit|413/i.test(message);

const createMarkdownPreviewComponents = (ownerPath: string, workingDirectory: string) => ({
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a
      {...props}
      href={typeof props.href === "string" ? rawFileUrl(resolveWorkspaceAssetPath(props.href, ownerPath, workingDirectory)) : props.href}
      target="_blank"
      rel="noreferrer"
      style={{ color: "var(--accent-primary)" }}
    />
  ),
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
});

const readFileSnapshot = async (path: string, workingDirectory: string): Promise<FileSnapshot | null> => {
  if (!isEditablePath(path)) return null;
  if (isDesktop()) {
    const desktopFile = await fsReadFileInfo(resolveDesktopFsPath(path, workingDirectory));
    if (desktopFile != null) {
      return {
        content: desktopFile.content,
        contentHash: desktopFile.contentHash ?? desktopFile.content_hash,
        sizeBytes: desktopFile.sizeBytes ?? desktopFile.size_bytes,
      };
    }
  }
  const response = await readWorkspaceFile(path);
  if (!response) return null;
  return {
    content: response.content,
    contentHash: response.content_hash,
    sizeBytes: response.size_bytes ?? response.size,
  };
};

export const EditorPanel = ({ chrome = "full" }: { chrome?: "full" | "minimal" } = {}) => {
  const themeMode = useAppStore((s) => s.themeMode);
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

  const closeOtherEditorTabs = useAppStore((s) => s.closeOtherEditorTabs);
  const closeAllEditorTabs = useAppStore((s) => s.closeAllEditorTabs);

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

  const activeTab = tabs.find((tab) => tab.path === activeTabPath) ?? null;
  const editorSlot = panelSlots.find((slot) => slot.kind === "editor");
  const editorSlotId = editorSlot?.id ?? "editor";
  const chatSlot = panelSlots.find((slot) => slot.kind === "chat");
  const dirty = activeTab ? activeTab.content !== activeTab.original : false;
  const language = useMemo(() => guessLanguage(activeTabPath ?? ""), [activeTabPath]);
  const monacoTheme = themeMode === "light" ? "light" : "vs-dark";
  const canRenderMarkdown = Boolean(activeTab && isMarkdownPath(activeTab.path) && !activeTab.loading && !activeTab.error && !activeTab.largeFile);
  const markdownImageCount = useMemo(
    () => canRenderMarkdown && activeTab ? countMarkdownPreviewImages(activeTab.content) : 0,
    [activeTab, canRenderMarkdown],
  );
  const markdownPreviewComponents = useMemo(
    () => createMarkdownPreviewComponents(activeTab?.path ?? "", workingDirectory),
    [activeTab?.path, workingDirectory],
  );
  const markdownPreviewTooImageHeavy = markdownImageCount > MAX_MARKDOWN_PREVIEW_IMAGES;
  const showEditorTabs = chrome === "full";
  const activeGitChange = useMemo(() => {
    if (!activeTabPath) return null;
    const files = [
      ...(gitChanges.live?.files ?? []),
      ...gitChanges.workingTree,
      ...gitChanges.staged,
    ];
    return files.find((file) => pathsMatch(file.path, activeTabPath) && file.patch) ?? null;
  }, [activeTabPath, gitChanges.live?.files, gitChanges.workingTree, gitChanges.staged]);

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
      if (!monacoMountedRef.current && useAppStore.getState().activeTabPath === path) {
        setMonacoUnavailable(true);
        console.warn("[EditorPanel] Monaco did not mount; falling back to the plain text editor.");
      }
    }, 2200);
    return () => window.clearTimeout(id);
  }, [activeTab?.path, activeTab?.loading, activeTab?.error, activeTab?.largeFile, mdPreview, monacoUnavailable]);

  // Consume open requests from other panels
  useEffect(() => {
    for (const request of editorOpenRequests) {
      openEditorTab(request.path);
      loadFileIfNeeded(request.path);
      handleSetActive(request.path, {
        path: request.path,
        line: request.line,
        column: request.column,
      });
      consumeEditorOpenRequest(request.id);
    }
  }, [editorOpenRequests, consumeEditorOpenRequest, openEditorTab]);

  // Sync activeEditorPath from workspace slice
  useEffect(() => {
    if (activeEditorPath && activeEditorPath !== activeTabPath) {
      const exists = tabs.some((tab) => tab.path === activeEditorPath);
      if (exists) setActiveTab(activeEditorPath);
    }
  }, [activeEditorPath, activeTabPath, tabs, setActiveTab]);

  // External file changes: track last processed index to handle batches
  const lastFileChangeLen = useRef(0);
  useEffect(() => {
    if (fileChanges.length <= lastFileChangeLen.current) {
      lastFileChangeLen.current = fileChanges.length;
      return;
    }
    const newChanges = fileChanges.slice(lastFileChangeLen.current);
    lastFileChangeLen.current = fileChanges.length;
    for (const change of newChanges) {
      markTabExternalChanged(change.path);
    }
  }, [fileChanges, markTabExternalChanged]);

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
    const epoch = (loadEpochRef.current.get(path) ?? 0) + 1;
    loadEpochRef.current.set(path, epoch);
    const directory = workingDirectory;
    const commit = (callback: () => void) => {
      if (loadEpochRef.current.get(path) !== epoch) return;
      if (directory !== useAppStore.getState().workingDirectory) return;
      if (!useAppStore.getState().editorTabs.some((tab) => tab.path === path)) return;
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
          }));
        } else {
          commit(() => markTabLoaded(path, snapshot.content, null, snapshot.contentHash, {
            largeFile: false,
            loadWarning: null,
            sizeBytes: snapshot.sizeBytes,
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
    const tab = useAppStore.getState().editorTabs.find((t) => t.path === path);
    if (tab?.loading) {
      void loadFileContent(path);
    }
  };

  const revealEditorTarget = (target: EditorTarget | null = pendingRevealRef.current) => {
    if (!target?.line || target.path !== activeTabPath) return false;
    const tab = useAppStore.getState().editorTabs.find((item) => item.path === target.path);
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
    if (pendingRevealRef.current?.path === target.path) {
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
      pushToast("No file is open.", "info", 1600);
      return;
    }
    if (activeTab.largeFile) {
      pushToast(`${basename(activeTab.path)} was not loaded into the editor.`, "warning", 2400);
      return;
    }
    if (!dirty) {
      setSaveStatus("saved");
      pushToast(`${basename(activeTab.path)} is already saved.`, "info", 1400);
      window.setTimeout(() => setSaveStatus("idle"), 1000);
      return;
    }
    if (saving) return;
    setSaving(true);
    setSaveStatus("idle");

    const expectedHash = activeTab.contentHash ?? "";
    const result = isDesktop()
      ? await fsCompareWriteFile(resolveDesktopFsPath(activeTab.path, workingDirectory), expectedHash, activeTab.content)
      : await compareWriteWorkspaceFile(activeTab.path, expectedHash, activeTab.content);
    if (result.ok) {
      const savedFile = result.file as { contentHash?: string; content_hash?: string };
      const nextHash = savedFile.contentHash ?? savedFile.content_hash;
      markTabSaved(activeTab.path, nextHash);
      setSaveStatus("saved");
      pushToast(`Saved ${basename(activeTab.path)}`, "success", 1600);
      window.setTimeout(() => setSaveStatus("idle"), 1400);
    } else {
      setSaveStatus("error");
      if (result.conflict) {
        markTabExternalChanged(activeTab.path);
        pushToast(`${basename(activeTab.path)} changed on disk. Save skipped to avoid overwriting it.`, "warning", 4200);
      } else {
        pushToast(result.message || `Save failed: ${basename(activeTab.path)}`, "error", 3500);
      }
    }
    setSaving(false);
  };

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
    const tab = tabs.find((t) => t.path === path);
    if (tab && tab.content !== tab.original) {
      const { showConfirm } = await import("../overlays/DialogService");
      const ok = await showConfirm({
        title: "Discard changes",
        message: `Discard changes to ${basename(path)}?`,
        confirmLabel: "Discard",
        danger: true,
      });
      if (!ok) return;
    }
    closeEditorTab(path);
  };

  useEffect(() => {
    const handleInsert = (event: Event) => {
      const detail = (event as EditorInsertEvent).detail;
      if (!detail?.text || !activeTab || activeTab.loading || activeTab.error || activeTab.largeFile || mdPreview) return;
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
            const active = tab.path === activeTabPath;
            return (
              <div
                key={tab.path}
                className="editor-tab relative inline-flex items-center gap-1.5 h-8 max-w-60 min-w-[124px] flex-none border border-transparent rounded-t-[7px] rounded-b-none cursor-pointer px-2.5 text-[13px] transition-[background,color,border-color] duration-100"
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
                  <Circle size={7} fill="currentColor" className="shrink-0" style={{ color: "var(--state-warning)" }} />
                ) : (
                  <span className="editor-tab-file-icon shrink-0" style={{ color: fileGlyphColor(tab.path) }} aria-hidden="true">
                    {fileIcon(tab.path, { size: 14, className: "editor-tab-file-icon-svg" })}
                  </span>
                )}
                <span className="overflow-hidden text-ellipsis whitespace-nowrap text-[13px]">
                  {basename(tab.path)}
                </span>
                </button>
                <button
                  type="button"
                  className="editor-tab-close inline-flex items-center justify-center rounded-[4px] w-[18px] h-[18px] shrink-0 ml-auto transition-[opacity,background] duration-100"
                  title="Close tab"
                  aria-label={`Close ${basename(tab.path)}`}
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
                  {tabDirty ? <Circle size={7} fill="currentColor" /> : <X size={14} />}
                </button>
              </div>
            );
          })}
        </div>
      )}
      {canRenderMarkdown && (
        <div className="flex items-center justify-between gap-2.5 min-h-[34px] px-2.5 py-[5px] border-b" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-page)" }}>
          <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>{basename(activeTab?.path ?? "Markdown")}</span>
          <div role="tablist" aria-label="Markdown view mode" className="inline-flex items-center gap-0.5 p-0.5 border rounded-[6px] shrink-0" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-soft)" }}>
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
                boxShadow: !mdPreview ? "0 1px 2px rgba(0,0,0,0.12)" : "none",
              }}
            >
              <Edit3 size={12} />
              Edit
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
                boxShadow: mdPreview ? "0 1px 2px rgba(0,0,0,0.12)" : "none",
              }}
            >
              <Eye size={12} />
              Rendered
            </button>
          </div>
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-hidden flex flex-col" style={{ background: "var(--surface-base)" }}>
        {activeTab ? (
          activeTab.loading ? (
            <div className="h-full grid place-items-center" style={{ color: "var(--text-muted)" }}>
              Loading file...
            </div>
          ) : activeTab.error ? (
            <FileLoadErrorNotice path={activeTab.path} error={activeTab.error} onRetry={() => {
              useAppStore.setState((state) => ({
                editorTabs: state.editorTabs.map((tab) => tab.path === activeTab.path
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
            <div className="md-prose editor-markdown-preview flex-1 overflow-y-auto px-[34px] py-6 text-[15px] leading-[1.7] break-words" style={{ color: "var(--text-primary)" }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownPreviewComponents} urlTransform={markdownUrlTransform}>
                {activeTab.content}
              </ReactMarkdown>
            </div>
          ) : monacoUnavailable ? (
            <PlainTextEditor
              value={activeTab.content}
              onChange={(value) => updateTabContent(activeTab.path, value)}
              onCursorChange={setCursor}
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
                  editor.onDidChangeCursorPosition((event) => {
                    setCursor({ line: event.position.lineNumber, column: event.position.column });
                  });
                  window.setTimeout(() => revealEditorTarget(), 0);
                }}
                options={{
                  automaticLayout: true,
                  fontFamily: "var(--font-mono)",
                  fontSize: 14,
                  lineHeight: 22,
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  wordWrap: "on",
                  fontLigatures: true,
                  cursorBlinking: "smooth",
                  cursorSmoothCaretAnimation: "on",
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
                  smoothScrolling: true,
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
            <span className="editor-empty-file-icon" aria-hidden="true">{fileIcon("file.ts", { size: 28, className: "editor-empty-file-icon-svg" })}</span>
            <div className="font-semibold" style={{ color: "var(--text-secondary)" }}>No file open</div>
            <div className="max-w-[420px] text-center">
              Use Files or the search box above to open a workspace file.
            </div>
          </div>
        )}
      </div>

      <div title={activeTabPath ?? ""} className="flex gap-3 min-h-6 items-center px-3 border-t overflow-hidden whitespace-nowrap text-xs" style={{ color: dirty ? "var(--state-warning)" : "var(--text-muted)", borderColor: "var(--border-subtle)", fontFamily: "var(--font-mono)", background: "var(--surface-sidebar)" }}>
        <span className="flex-1 min-w-0 overflow-hidden text-ellipsis">
          {activeTabPath || "No file open"}{dirty ? " - modified" : ""}
        </span>
        {activeTab?.sizeBytes != null && <span>{formatBytes(activeTab.sizeBytes)}</span>}
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
              fontWeight: 650,
            }}
          >
            <GitCompare size={12} />
            Diff
            <span style={{ color: "var(--state-success)" }}>+{activeGitChange.additions}</span>
            <span style={{ color: "var(--state-danger)" }}>-{activeGitChange.deletions}</span>
          </button>
        )}
        {saveStatus === "saved" && <span style={{ color: "var(--state-success)" }}>Saved</span>}
        {saveStatus === "error" && <span style={{ color: "var(--state-danger)" }}>Save failed</span>}
        <span>{`Ln ${cursor.line}, Col ${cursor.column}`}</span>
      </div>

      {ctxMenu && (
        <ContextMenu
          position={{ x: ctxMenu.x, y: ctxMenu.y }}
          onClose={() => setCtxMenu(null)}
          items={[
            { label: "Close tab", onClick: () => handleCloseTab(ctxMenu.path) },
            { label: "Close other tabs", onClick: () => closeOtherEditorTabs(ctxMenu.path) },
            { label: "Close all tabs", onClick: () => closeAllEditorTabs() },
            {
              label: "Close to the right",
              onClick: () => {
                const idx = tabs.findIndex((t) => t.path === ctxMenu.path);
                tabs.slice(idx + 1).forEach((t) => closeEditorTab(t.path));
              },
            },
            { separator: true, label: "" },
            {
              label: "Copy file path",
              onClick: () => { void navigator.clipboard.writeText(ctxMenu.path); },
            },
            {
              label: "Reveal in file tree",
              onClick: () => { void revealPath(ctxMenu.path); },
            },
          ]}
        />
      )}
    </div>
  );
};

const EditorLoading = () => (
  <div className="h-full grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
    Loading editor...
  </div>
);

const PlainTextEditor = ({ value, onChange, onCursorChange }: PlainTextEditorProps) => {
  const updateCursor = (target: HTMLTextAreaElement) => {
    onCursorChange(cursorFromOffset(target.value, target.selectionStart ?? 0));
  };
  return (
    <textarea
      aria-label="Plain text editor"
      value={value}
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
  fontSize: 14,
  lineHeight: "22px",
  whiteSpace: "pre",
  overflow: "auto",
  tabSize: 2,
};

const LargeFileNotice = ({ tab }: { tab: { path: string; loadWarning?: string | null; sizeBytes?: number } }) => (
  <div className="h-full flex flex-col items-center justify-center gap-[9px] p-6 text-center" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
    <FileWarning size={28} style={{ color: "var(--state-warning)" }} />
    <div className="font-bold" style={{ color: "var(--text-primary)" }}>File not loaded into editor</div>
    <div className="max-w-[520px] leading-[1.5]" style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)" }}>
      {tab.loadWarning || "This file is too large to render safely in the editor."}
    </div>
    <div className="max-w-[520px] overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
      {basename(tab.path)}{tab.sizeBytes != null ? ` - ${formatBytes(tab.sizeBytes)}` : ""}
    </div>
  </div>
);

const FileLoadErrorNotice = ({ path, error, onRetry }: { path: string; error: string; onRetry: () => void }) => (
  <div className="h-full flex flex-col items-center justify-center gap-[9px] p-6 text-center" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
    <FileWarning size={28} style={{ color: "var(--state-danger)" }} />
    <div className="font-bold" style={{ color: "var(--text-primary)" }}>File could not be loaded</div>
    <div className="max-w-[560px] leading-[1.5]" style={{ color: "var(--state-danger)", fontSize: "var(--text-sm)" }}>
      {error}
    </div>
    <div className="max-w-[560px] overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
      {path}
    </div>
    <button type="button" onClick={onRetry} className="inline-flex items-center gap-1.5 h-[30px] px-2.5 border rounded-[4px] cursor-pointer font-semibold" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-raised)", color: "var(--text-primary)", fontFamily: "var(--font-ui)", fontSize: "var(--text-xs)" }}>
      Retry
    </button>
  </div>
);

const MarkdownPreviewLimitNotice = ({ imageCount, onEdit }: { imageCount: number; onEdit: () => void }) => (
  <div className="h-full flex flex-col items-center justify-center gap-[9px] p-6 text-center" style={{ color: "var(--text-muted)", background: "var(--surface-base)" }}>
    <Image size={28} style={{ color: "var(--state-warning)" }} />
    <div className="font-bold" style={{ color: "var(--text-primary)" }}>Markdown preview skipped</div>
    <div className="max-w-[520px] leading-[1.5]" style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)" }}>
      This Markdown file references {imageCount.toLocaleString()} images. Open it in edit mode to avoid loading them all at once.
    </div>
    <button type="button" onClick={onEdit} className="inline-flex items-center gap-1.5 h-[30px] px-2.5 border rounded-[4px] cursor-pointer font-semibold" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-raised)", color: "var(--text-primary)", fontFamily: "var(--font-ui)", fontSize: "var(--text-xs)" }}>
      <Edit3 size={13} />
      Edit Markdown
    </button>
  </div>
);

const ImageViewer = ({ path, workingDirectory }: { path: string; workingDirectory: string }) => {
  const imgSrc = rawFileUrl(path, workingDirectory);

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-3 p-6 overflow-auto" style={{ background: "var(--surface-base)" }}>
      <img
        src={imgSrc}
        alt={basename(path)}
        className="max-w-full object-contain rounded-[4px] border"
        style={{
          maxHeight: "calc(100% - 40px)",
          borderColor: "var(--border-subtle)",
        }}
        onError={(e) => {
          const target = e.currentTarget;
          target.style.display = "none";
          const fallback = target.nextElementSibling as HTMLElement | null;
          if (fallback) fallback.style.display = "flex";
        }}
      />
      <div className="hidden flex-col items-center gap-2" style={{ color: "var(--text-muted)" }}>
        <Image size={32} />
        <span style={{ fontSize: "var(--text-sm)" }}>Cannot display image</span>
        <span style={{ fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)" }}>{basename(path)}</span>
      </div>
      <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
        {basename(path)}
      </span>
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
