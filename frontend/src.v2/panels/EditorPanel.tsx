import { useEffect, useMemo, useRef, useState } from "react";
import { lazy, Suspense } from "react";
import { Circle, Edit3, Eye, FileCode2, FileWarning, Image, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useAppStore } from "../stores";
import { apiBase, withRuntimeToken } from "../protocol/api";
import {
  compareWriteWorkspaceFile,
  readWorkspaceFile,
} from "../protocol/workspace";
import { fsCompareWriteFile, fsReadFileInfo, isDesktop, revealPath } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";

const LazyMonacoEditor = lazy(() => import("@monaco-editor/react"));

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

const FILE_ICON_COLORS: Record<string, string> = {
  ts: "#3178c6", tsx: "#3178c6",
  js: "#f7df1e", jsx: "#f7df1e",
  py: "#3572a5",
  json: "#cb8622",
  md: "#519aba",
  css: "#563d7c", scss: "#c6538c",
  html: "#e34c26",
  yml: "#cb171e", yaml: "#cb171e",
  toml: "#9c4221",
  rs: "#dea584",
  go: "#00add8",
  sh: "#89e051", bash: "#89e051",
};

const getFileIconColor = (path: string): string => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return FILE_ICON_COLORS[ext] ?? "var(--text-muted)";
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

const isImagePath = (path: string): boolean => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return IMAGE_EXTENSIONS.has(ext);
};

const isMarkdownPath = (path: string): boolean => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return ext === "md" || ext === "mdx";
};

const isEditablePath = (path: string): boolean => {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return !UNSUPPORTED_EDITOR_EXTENSIONS.has(ext);
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

const markdownPreviewComponents = {
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a {...props} target="_blank" rel="noreferrer" style={{ color: "var(--accent-primary)" }} />
  ),
  img: (props: React.ImgHTMLAttributes<HTMLImageElement>) => (
    <img
      {...props}
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
  ),
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
};

const readFileSnapshot = async (path: string): Promise<FileSnapshot | null> => {
  if (!isEditablePath(path)) return null;
  if (isDesktop()) {
    const desktopFile = await fsReadFileInfo(path);
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

export const EditorPanel = () => {
  const themeMode = useAppStore((s) => s.themeMode);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const editorOpenRequests = useAppStore((s) => s.editorOpenRequests);
  const activeEditorPath = useAppStore((s) => s.activeEditorPath);
  const fileChanges = useAppStore((s) => s.fileChanges);
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
  const editorRef = useRef<MonacoEditorInstance | null>(null);
  const pendingRevealRef = useRef<EditorTarget | null>(null);

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
  const markdownPreviewTooImageHeavy = markdownImageCount > MAX_MARKDOWN_PREVIEW_IMAGES;

  useEffect(() => {
    setMdPreview(false);
  }, [activeTabPath]);

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
    if (isImagePath(path)) {
      markTabLoaded(path, "", null);
      return;
    }
    if (!isEditablePath(path)) {
      markTabLoaded(path, "", `${basename(path)} is not a text file that can be edited here.`);
      return;
    }
    try {
      const snapshot = await readFileSnapshot(path);
      if (snapshot != null) {
        const warning = largeFileReason(snapshot);
        if (warning) {
          markTabLoaded(path, "", null, snapshot.contentHash, {
            largeFile: true,
            loadWarning: warning,
            sizeBytes: snapshot.sizeBytes,
          });
        } else {
          markTabLoaded(path, snapshot.content, null, snapshot.contentHash, {
            largeFile: false,
            loadWarning: null,
            sizeBytes: snapshot.sizeBytes,
          });
        }
      } else {
        markTabLoaded(path, "", `Could not read ${path}`);
      }
    } catch (error) {
      const message = errorMessage(error);
      if (isLargeFileError(message)) {
        markTabLoaded(path, "", null, undefined, {
          largeFile: true,
          loadWarning: message,
          sizeBytes: undefined,
        });
      } else {
        markTabLoaded(path, "", message || `Could not read ${path}`);
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
      ? await fsCompareWriteFile(activeTab.path, expectedHash, activeTab.content)
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
      {tabs.length > 0 && (
        <div className="flex min-h-[38px] overflow-x-auto overflow-y-hidden gap-0.5 px-2.5 pt-1.5 pb-0 border-b scrollbar-thin" style={{ borderColor: "var(--border-subtle)", background: "var(--surface-sidebar)", scrollbarColor: "color-mix(in oklch, var(--text-muted) 35%, transparent) transparent" }}>
          {tabs.map((tab) => {
            const tabDirty = tab.content !== tab.original;
            const active = tab.path === activeTabPath;
            return (
              <button
                key={tab.path}
                className="editor-tab relative inline-flex items-center gap-1.5 h-8 max-w-60 min-w-[124px] flex-none border border-transparent rounded-t-[7px] rounded-b-none cursor-pointer px-2.5 text-[13px] transition-[background,color,border-color] duration-100"
                onClick={() => handleSetActive(tab.path)}
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
                {tabDirty ? (
                  <Circle size={7} fill="currentColor" className="shrink-0" style={{ color: "var(--state-warning)" }} />
                ) : (
                  <FileCode2 size={13} className="shrink-0" style={{ color: getFileIconColor(tab.path) }} />
                )}
                <span className="overflow-hidden text-ellipsis whitespace-nowrap text-[13px]">
                  {basename(tab.path)}
                </span>
                <span
                  role="button"
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
                </span>
              </button>
            );
          })}
        </div>
      )}
      {activeTab?.error && (
        <div className="px-2.5 py-1.5" style={{ color: "var(--state-danger)", fontSize: "var(--text-xs)" }}>
          {activeTab.error}
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
          ) : isImagePath(activeTab.path) ? (
            <ImageViewer path={activeTab.path} workingDirectory={workingDirectory} />
          ) : activeTab.largeFile ? (
            <LargeFileNotice tab={activeTab} />
          ) : canRenderMarkdown && mdPreview && markdownPreviewTooImageHeavy ? (
            <MarkdownPreviewLimitNotice
              imageCount={markdownImageCount}
              onEdit={() => setMdPreview(false)}
            />
          ) : canRenderMarkdown && mdPreview ? (
            <div className="md-prose flex-1 overflow-y-auto px-[34px] py-6 text-[15px] leading-[1.7] break-words" style={{ color: "var(--text-primary)" }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownPreviewComponents}>
                {activeTab.content}
              </ReactMarkdown>
            </div>
          ) : (
            <Suspense fallback={<EditorLoading />}>
              <LazyMonacoEditor
                height="100%"
                language={language}
                theme={monacoTheme}
                value={activeTab.content}
                onChange={(value) => updateTabContent(activeTab.path, value ?? "")}
                onMount={(editor) => {
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
            <FileCode2 size={30} />
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
        {saveStatus === "saved" && <span style={{ color: "var(--state-success)" }}>Saved</span>}
        {saveStatus === "error" && <span style={{ color: "var(--state-danger)" }}>Save failed</span>}
        <span>Ln {cursor.line}, Col {cursor.column}</span>
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
  const imgSrc = (() => {
    const normalized = path.replace(/\\/g, "/");
    if (normalized.startsWith("http://") || normalized.startsWith("https://")) return normalized;
    return withRuntimeToken(`${apiBase()}/api/workspace/raw?path=${encodeURIComponent(path)}`);
  })();

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

// TabContextMenu used to be defined inline here; now replaced by the
// generic ContextMenu component in ../components/ContextMenu.
