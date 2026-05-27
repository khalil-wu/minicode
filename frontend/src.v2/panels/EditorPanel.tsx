import { useEffect, useMemo, useRef, useState } from "react";
import { lazy, Suspense } from "react";
import { Check, Circle, Eye, EyeOff, FileCode2, FolderOpen, Image, Maximize2, Minimize2, RotateCcw, Save, Search, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useAppStore } from "../stores";
import { apiBase, withRuntimeToken } from "../protocol/api";
import {
  compareWriteWorkspaceFile,
  readWorkspaceFile,
  searchWorkspaceFiles,
} from "../protocol/workspace";
import { fsCompareWriteFile, fsReadFileInfo, fsSearchFiles, isDesktop, revealPath } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";

const LazyMonacoEditor = lazy(() => import("@monaco-editor/react"));

type MonacoEditorInstance = {
  getSelection: () => unknown;
  executeEdits: (source: string, edits: Array<{ range: unknown; text: string; forceMoveMarkers?: boolean }>) => void;
  focus: () => void;
};

type EditorInsertEvent = CustomEvent<{ text: string; handled?: boolean }>;

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
const breadcrumbs = (path: string): string[] => path.replace(/\\/g, "/").split("/").filter(Boolean);

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
}

const readFileSnapshot = async (path: string): Promise<FileSnapshot | null> => {
  if (!isEditablePath(path)) return null;
  if (isDesktop()) {
    const desktopFile = await fsReadFileInfo(path);
    if (desktopFile != null) {
      return {
        content: desktopFile.content,
        contentHash: desktopFile.contentHash ?? desktopFile.content_hash,
      };
    }
  }
  const response = await readWorkspaceFile(path);
  if (!response) return null;
  return {
    content: response.content,
    contentHash: response.content_hash,
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

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{ path: string; name: string }[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saved" | "error">("idle");
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; path: string } | null>(null);
  const [mdPreview, setMdPreview] = useState(false);
  const editorRef = useRef<MonacoEditorInstance | null>(null);

  const activeTab = tabs.find((tab) => tab.path === activeTabPath) ?? null;
  const editorSlot = panelSlots.find((slot) => slot.kind === "editor");
  const editorSlotId = editorSlot?.id ?? "editor";
  const chatSlot = panelSlots.find((slot) => slot.kind === "chat");
  const dirty = activeTab ? activeTab.content !== activeTab.original : false;
  const language = useMemo(() => guessLanguage(activeTabPath ?? ""), [activeTabPath]);
  const monacoTheme = themeMode === "light" ? "light" : "vs-dark";
  const activeFileName = activeTabPath ? basename(activeTabPath) : "";

  useEffect(() => {
    if (activeTabPath && isMarkdownPath(activeTabPath)) {
      setMdPreview(true);
    } else {
      setMdPreview(false);
    }
  }, [activeTabPath]);

  // Search
  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      return;
    }
    setSearchLoading(true);
    const timer = window.setTimeout(() => {
      const search = isDesktop()
        ? fsSearchFiles(workingDirectory || "", query.trim(), 20, "file")
        : searchWorkspaceFiles(query.trim(), 20);
      search
        .then((items) => setResults(items))
        .catch(() => setResults([]))
        .finally(() => setSearchLoading(false));
    }, 150);
    return () => window.clearTimeout(timer);
  }, [query, workingDirectory]);

  // Consume open requests from other panels
  useEffect(() => {
    for (const requestedPath of editorOpenRequests) {
      openEditorTab(requestedPath);
      loadFileIfNeeded(requestedPath);
      consumeEditorOpenRequest(requestedPath);
    }
  }, [editorOpenRequests, consumeEditorOpenRequest, openEditorTab]);

  // Sync activeEditorPath from workspace slice
  useEffect(() => {
    if (activeEditorPath && activeEditorPath !== activeTabPath) {
      const exists = tabs.some((tab) => tab.path === activeEditorPath);
      if (exists) setActiveTab(activeEditorPath);
    }
  }, [activeEditorPath, activeTabPath, tabs, setActiveTab]);

  // External file changes — track last processed index to handle batches
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
    const snapshot = await readFileSnapshot(path);
    if (snapshot != null) {
      markTabLoaded(path, snapshot.content, null, snapshot.contentHash);
    } else {
      markTabLoaded(path, "", `Could not read ${path}`);
    }
  };

  const loadFileIfNeeded = (path: string) => {
    const tab = useAppStore.getState().editorTabs.find((t) => t.path === path);
    if (tab?.loading) {
      void loadFileContent(path);
    }
  };

  const handleSetActive = (path: string) => {
    setActiveTab(path);
    setCursor({ line: 1, column: 1 });
    useAppStore.setState({ activeEditorPath: path });
  };

  const openFile = (targetPath: string) => {
    const normalized = targetPath.trim();
    if (!normalized) return;
    openEditorTab(normalized);
    loadFileIfNeeded(normalized);
    handleSetActive(normalized);
    setQuery("");
    setResults([]);
  };

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
      if (!detail?.text || !activeTab || activeTab.loading || activeTab.error || mdPreview) return;
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
    <div style={editorShellStyle}>
      <div style={editorTopBarStyle}>
        <div style={editorToolbarMainStyle}>
          <div style={quickOpenStyle}>
            <Search size={13} />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && query.trim()) openFile(results[0]?.path ?? query.trim());
              }}
              placeholder="Search files or type path"
              style={quickOpenInputStyle}
            />
          </div>
          <div style={editorActionsStyle}>
            {activeTabPath && isMarkdownPath(activeTabPath) && (
              <IconButton label={mdPreview ? "Edit source" : "Preview markdown"} onClick={() => setMdPreview(!mdPreview)} primary={mdPreview}>
                <Eye size={14} />
              </IconButton>
            )}
            <IconButton label="Save file" disabled={!dirty || saving} onClick={() => void save()} primary={dirty}>
              {saveStatus === "saved" ? <Check size={14} /> : <Save size={14} />}
            </IconButton>
            <IconButton label="Revert file" disabled={!dirty || saving} onClick={revert}>
              <RotateCcw size={14} />
            </IconButton>
            <IconButton label="Hide editor" onClick={hideEditor}>
              <EyeOff size={14} />
            </IconButton>
            <IconButton
              label={editorSlot?.maximized ? "Restore editor" : "Maximize editor"}
              onClick={() => togglePanelMaximized(editorSlotId)}
              primary={Boolean(editorSlot?.maximized)}
            >
              {editorSlot?.maximized ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            </IconButton>
            <IconButton label="Close editor" onClick={closeEditorPanel}>
              <X size={14} />
            </IconButton>
          </div>
        </div>
        {activeTabPath && (
          <div style={editorContextBarStyle}>
            <div style={activeFileBadgeStyle} title={activeTabPath}>
              <FileCode2 size={15} style={{ color: getFileIconColor(activeTabPath) }} />
              <span style={activeFileNameStyle}>{activeFileName}</span>
              {dirty && <span style={modifiedPillStyle}>modified</span>}
            </div>
            <div title={activeTabPath} style={breadcrumbStyle}>
              {breadcrumbs(activeTabPath).slice(-5).map((part, index, parts) => (
                <span key={`${part}-${index}`} style={{ display: "inline-flex", alignItems: "center", gap: 5, minWidth: 0 }}>
                  {index === 0 && <FolderOpen size={12} />}
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{part}</span>
                  {index < parts.length - 1 && <span style={{ color: "var(--text-muted)" }}>/</span>}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {results.length > 0 && (
        <div style={quickOpenResultsStyle}>
          {results.map((result) => (
            <button
              key={result.path}
              onClick={() => openFile(result.path)}
              style={quickOpenResultStyle}
            >
              {result.path}
            </button>
          ))}
        </div>
      )}
      {searchLoading && (
        <div style={{ padding: "4px 10px", color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>
          Searching...
        </div>
      )}

      {tabs.length > 0 && (
        <div style={tabStripStyle}>
          {tabs.map((tab) => {
            const tabDirty = tab.content !== tab.original;
            const active = tab.path === activeTabPath;
            return (
              <button
                key={tab.path}
                className="editor-tab"
                onClick={() => handleSetActive(tab.path)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  setCtxMenu({ x: e.clientX, y: e.clientY, path: tab.path });
                }}
                title={tab.path}
                style={tabButtonStyle(active)}
              >
                {tabDirty ? (
                  <Circle size={7} fill="currentColor" style={{ color: "var(--state-warning)", flexShrink: 0 }} />
                ) : (
                  <FileCode2 size={13} style={{ color: getFileIconColor(tab.path), flexShrink: 0 }} />
                )}
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: "13px" }}>
                  {basename(tab.path)}
                </span>
                <span
                  role="button"
                  className="editor-tab-close"
                  title="Close tab"
                  aria-label={`Close ${basename(tab.path)}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    handleCloseTab(tab.path);
                  }}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "var(--text-muted)",
                    borderRadius: "var(--radius-sm, 4px)",
                    width: 18,
                    height: 18,
                    flexShrink: 0,
                    marginLeft: "auto",
                    opacity: active || tabDirty ? 1 : 0,
                    transition: "opacity 100ms, background 100ms",
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
        <div style={{ padding: "6px 10px", color: "var(--state-danger)", fontSize: "var(--text-xs)" }}>
          {activeTab.error}
        </div>
      )}

      <div style={editorCanvasStyle}>
        {activeTab ? (
          activeTab.loading ? (
            <div style={{ height: "100%", display: "grid", placeItems: "center", color: "var(--text-muted)" }}>
              Loading file...
            </div>
          ) : isImagePath(activeTab.path) ? (
            <ImageViewer path={activeTab.path} workingDirectory={workingDirectory} />
          ) : isMarkdownPath(activeTab.path) && mdPreview ? (
            <div style={mdPreviewStyle}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{activeTab.content}</ReactMarkdown>
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
          <div style={emptyEditorStyle}>
            <FileCode2 size={30} />
            <div style={{ color: "var(--text-secondary)", fontWeight: 600 }}>No file open</div>
            <div style={{ maxWidth: 420, textAlign: "center" }}>
              Use Files or the search box above to open a workspace file.
            </div>
          </div>
        )}
      </div>

      <div title={activeTabPath ?? ""} style={editorInfoBarStyle(dirty)}>
        <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
          {activeTabPath || "No file open"}{dirty ? "  • modified" : ""}
        </span>
        <span>{language}</span>
        {saveStatus === "saved" && <span style={{ color: "var(--state-success)" }}>Saved</span>}
        {saveStatus === "error" && <span style={{ color: "var(--state-danger)" }}>Save failed</span>}
        <span>Ln {cursor.line}, Col {cursor.column}</span>
      </div>

      {ctxMenu && (
        <TabContextMenu
          x={ctxMenu.x}
          y={ctxMenu.y}
          onClose={() => setCtxMenu(null)}
          onCloseTab={() => { handleCloseTab(ctxMenu.path); setCtxMenu(null); }}
          onCloseOthers={() => { closeOtherEditorTabs(ctxMenu.path); setCtxMenu(null); }}
          onCloseAll={() => { closeAllEditorTabs(); setCtxMenu(null); }}
          onCloseToRight={() => {
            const idx = tabs.findIndex((t) => t.path === ctxMenu.path);
            tabs.slice(idx + 1).forEach((t) => closeEditorTab(t.path));
            setCtxMenu(null);
          }}
          onCopyPath={() => { void navigator.clipboard.writeText(ctxMenu.path); setCtxMenu(null); }}
          onReveal={() => { void revealPath(ctxMenu.path); setCtxMenu(null); }}
        />
      )}
    </div>
  );
};

const EditorLoading = () => (
  <div style={{ height: "100%", display: "grid", placeItems: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
    Loading editor...
  </div>
);

const IconButton = ({
  children,
  disabled,
  label,
  onClick,
  primary = false,
}: {
  children: React.ReactNode;
  disabled?: boolean;
  label: string;
  onClick: () => void;
  primary?: boolean;
}) => (
  <button
    disabled={disabled}
    onClick={onClick}
    title={label}
    aria-label={label}
    style={{
      width: 28,
      height: 28,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      background: primary && !disabled ? "var(--accent-primary)" : "var(--surface-page)",
      color: primary && !disabled ? "var(--surface-base)" : disabled ? "var(--text-muted)" : "var(--text-primary)",
      border: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-sm, 6px)",
      cursor: disabled ? "not-allowed" : "pointer",
      padding: 0,
      opacity: disabled ? 0.55 : 1,
    }}
  >
    {children}
  </button>
);

const editorShellStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
  background: "var(--surface-page)",
};

const editorTopBarStyle: React.CSSProperties = {
  display: "grid",
  gap: 9,
  minHeight: 70,
  padding: "10px 14px 9px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
  fontSize: "var(--text-sm)",
};

const editorToolbarMainStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
};

const editorActionsStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  marginLeft: "auto",
  flexShrink: 0,
};

const quickOpenStyle: React.CSSProperties = {
  height: 32,
  flex: "1 1 360px",
  minWidth: 170,
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "0 10px",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  color: "var(--text-muted)",
};

const quickOpenInputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  background: "transparent",
  border: 0,
  color: "var(--text-primary)",
  outline: 0,
  fontSize: "14px",
};

const quickOpenResultsStyle: React.CSSProperties = {
  borderBottom: "1px solid var(--border-subtle)",
  maxHeight: 180,
  overflowY: "auto",
  padding: 4,
  background: "var(--surface-sidebar)",
};

const quickOpenResultStyle: React.CSSProperties = {
  width: "100%",
  textAlign: "left",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 4px)",
  background: "transparent",
  color: "var(--text-secondary)",
  cursor: "pointer",
  padding: "5px 7px",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-xs)",
};

const editorContextBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minWidth: 0,
};

const activeFileBadgeStyle: React.CSSProperties = {
  flex: "0 0 auto",
  maxWidth: "46%",
  minWidth: 0,
  height: 28,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  padding: "0 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-soft)",
};

const activeFileNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-primary)",
  fontSize: "13px",
  fontWeight: 650,
};

const modifiedPillStyle: React.CSSProperties = {
  flexShrink: 0,
  padding: "1px 5px",
  borderRadius: 999,
  background: "color-mix(in oklch, var(--state-warning) 14%, transparent)",
  color: "var(--state-warning)",
  fontSize: 10,
  fontFamily: "var(--font-mono)",
};

const breadcrumbStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 5,
  minWidth: 0,
  flex: 1,
  height: 28,
  padding: "0 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-page)",
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  overflow: "hidden",
  whiteSpace: "nowrap",
};

const tabStripStyle: React.CSSProperties = {
  display: "flex",
  minHeight: 38,
  overflowX: "auto",
  overflowY: "hidden",
  gap: 2,
  padding: "6px 10px 0",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
  scrollbarWidth: "thin",
  scrollbarColor: "color-mix(in oklch, var(--text-muted) 35%, transparent) transparent",
};

const tabButtonStyle = (active: boolean): React.CSSProperties => ({
  position: "relative",
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  height: 32,
  maxWidth: 240,
  minWidth: 124,
  flex: "0 0 auto",
  border: "1px solid transparent",
  borderBottomColor: active ? "var(--surface-base)" : "transparent",
  borderRadius: "var(--radius-sm, 7px) var(--radius-sm, 7px) 0 0",
  background: active ? "var(--surface-base)" : "transparent",
  color: active ? "var(--text-primary)" : "var(--text-muted)",
  cursor: "pointer",
  padding: "0 10px",
  fontSize: "13px",
  fontFamily: "var(--font-ui)",
  transition: "background var(--transition-micro, 100ms), color var(--transition-micro, 100ms), border-color var(--transition-micro, 100ms)",
});

const editorInfoBarStyle = (dirty: boolean): React.CSSProperties => ({
  display: "flex",
  gap: 12,
  minHeight: 24,
  alignItems: "center",
  padding: "0 12px",
  color: dirty ? "var(--state-warning)" : "var(--text-muted)",
  borderTop: "1px solid var(--border-subtle)",
  fontFamily: "var(--font-mono)",
  fontSize: "12px",
  overflow: "hidden",
  whiteSpace: "nowrap",
  background: "var(--surface-sidebar)",
});

const editorCanvasStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
  background: "var(--surface-base)",
};

const emptyEditorStyle: React.CSSProperties = {
  height: "100%",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  gap: 10,
  color: "var(--text-muted)",
  fontSize: "14px",
  background: "var(--surface-base)",
};

const mdPreviewStyle: React.CSSProperties = {
  flex: 1,
  overflowY: "auto",
  padding: "24px 34px",
  color: "var(--text-primary)",
  fontSize: "15px",
  lineHeight: 1.7,
  wordBreak: "break-word",
};

const ImageViewer = ({ path, workingDirectory }: { path: string; workingDirectory: string }) => {
  const imgSrc = (() => {
    const normalized = path.replace(/\\/g, "/");
    if (normalized.startsWith("http://") || normalized.startsWith("https://")) return normalized;
    return withRuntimeToken(`${apiBase()}/api/workspace/raw?path=${encodeURIComponent(path)}`);
  })();

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        padding: 24,
        overflow: "auto",
        background: "var(--surface-base)",
      }}
    >
      <img
        src={imgSrc}
        alt={basename(path)}
        style={{
          maxWidth: "100%",
          maxHeight: "calc(100% - 40px)",
          objectFit: "contain",
          borderRadius: "var(--radius-sm, 4px)",
          border: "1px solid var(--border-subtle)",
        }}
        onError={(e) => {
          const target = e.currentTarget;
          target.style.display = "none";
          const fallback = target.nextElementSibling as HTMLElement | null;
          if (fallback) fallback.style.display = "flex";
        }}
      />
      <div
        style={{
          display: "none",
          flexDirection: "column",
          alignItems: "center",
          gap: 8,
          color: "var(--text-muted)",
        }}
      >
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

const TabContextMenu = ({
  x,
  y,
  onClose,
  onCloseTab,
  onCloseOthers,
  onCloseAll,
  onCloseToRight,
  onCopyPath,
  onReveal,
}: {
  x: number;
  y: number;
  onClose: () => void;
  onCloseTab: () => void;
  onCloseOthers: () => void;
  onCloseAll: () => void;
  onCloseToRight: () => void;
  onCopyPath: () => void;
  onReveal: () => void;
}) => {
  useEffect(() => {
    const dismiss = () => onClose();
    window.addEventListener("mousedown", dismiss);
    return () => window.removeEventListener("mousedown", dismiss);
  }, [onClose]);

  const items = [
    { label: "Close", action: onCloseTab },
    { label: "Close Others", action: onCloseOthers },
    { label: "Close All", action: onCloseAll },
    { label: "Close to the Right", action: onCloseToRight },
    { label: "---", action: () => {} },
    { label: "Copy Path", action: onCopyPath },
    { label: "Reveal in Explorer", action: onReveal },
  ];

  return (
    <div
      onMouseDown={(e) => e.stopPropagation()}
      style={{
        position: "fixed",
        left: x,
        top: y,
        zIndex: 9999,
        background: "var(--surface-raised)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm, 6px)",
        boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
        padding: "4px 0",
        minWidth: 160,
      }}
    >
      {items.map((item, i) =>
        item.label === "---" ? (
          <div key={i} style={{ height: 1, background: "var(--border-subtle)", margin: "4px 0" }} />
        ) : (
          <button
            key={item.label}
            onClick={item.action}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              background: "transparent",
              border: 0,
              color: "var(--text-secondary)",
              cursor: "pointer",
              padding: "5px 12px",
              fontSize: "var(--text-xs)",
            }}
          >
            {item.label}
          </button>
        ),
      )}
    </div>
  );
};
