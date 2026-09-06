import { useEffect, useRef, useState } from "react";
import { FileText } from "../lib/icons";
import { X } from "lucide-react";
import { useAppStore } from "../stores";
import { isDesktop, fsSearchFiles } from "../desktop/runtime";
import { searchWorkspaceFiles } from "../protocol/workspace";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { workspaceRootsEqual } from "../lib/workspace-path";

const fileNameOf = (path: string) => path.split(/[\\/]/).pop() || path;

export const QuickOpen = () => {
  const visible = useAppStore((s) => s.quickOpenVisible);
  const storeResults = useAppStore((s) => s.quickOpenResults);
  const storeLoading = useAppStore((s) => s.quickOpenLoading);
  const editorTabs = useAppStore((s) => s.editorTabs);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const enabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const [searchError, setSearchError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useFocusTrap(visible && enabled);

  useEffect(() => {
    if (visible && !enabled) {
      useAppStore.setState({ quickOpenVisible: false, quickOpenResults: [], quickOpenLoading: false });
      return;
    }
    if (visible) {
      setQuery("");
      setActiveIdx(0);
      setSearchError("");
      useAppStore.setState({ quickOpenResults: [], quickOpenLoading: false });
    }
  }, [visible, enabled]);

  useEffect(() => {
    let cancelled = false;
    const isCurrent = () => !cancelled
      && useAppStore.getState().quickOpenVisible
      && workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory);
    const requestedQuery = query.trim();
    setSearchError("");
    setActiveIdx(0);
    if (!visible || !enabled || !requestedQuery || !workingDirectory) {
      useAppStore.setState({ quickOpenResults: [], quickOpenLoading: false });
      return;
    }
    useAppStore.setState({ quickOpenResults: [], quickOpenLoading: true });
    const timer = window.setTimeout(() => {
      const request = isDesktop()
        ? fsSearchFiles(workingDirectory, requestedQuery, 20, "file")
        : searchWorkspaceFiles(workingDirectory, requestedQuery, 20, "file");
      request.then((results) => {
        if (isCurrent()) {
          useAppStore.setState({ quickOpenResults: results, quickOpenLoading: false });
        }
      }).catch((error: unknown) => {
        if (!isCurrent()) return;
        setSearchError(error instanceof Error ? error.message : "文件搜索失败，请重试。");
        useAppStore.setState({ quickOpenResults: [], quickOpenLoading: false });
      });
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query, workingDirectory, visible, enabled]);

  const close = () => {
    useAppStore.setState({ quickOpenVisible: false, quickOpenLoading: false });
  };

  const openFile = (file: { path: string; name: string }) => {
    useAppStore.getState().openEditorFile(file.path, file.name);
    close();
  };

  const mentionFile = (file: { path: string; name: string }) => {
    useAppStore.getState().addSelectedMention({
      kind: "file",
      path: file.path,
      name: file.name,
    });
    close();
  };

  if (!visible || !enabled) return null;

  // Empty query falls back to the files already open in the editor — a
  // "recents" section without new state, since tabs ARE the recents.
  const showingOpenTabs = !query.trim();
  const results = showingOpenTabs
    ? editorTabs.map((tab) => ({ path: tab.path, name: fileNameOf(tab.path) }))
    : storeResults;

  return (
    <div
      className="overlay-backdrop"
      onClick={close}
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--backdrop-overlay)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        padding: "10vh 16px 16px",
        zIndex: "var(--z-modal)",
        pointerEvents: "auto",
      }}
    >
      <div
        ref={dialogRef}
        className="quick-open-surface"
        role="dialog"
        aria-modal="true"
        aria-label="快速打开文件"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            close();
          }
        }}
        style={{
          width: "min(600px, 100%)",
          maxHeight: "calc(90dvh - 16px)",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--shadow-strong-overlay)",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          pointerEvents: "auto",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", flexShrink: 0 }}>
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          role="combobox"
          aria-label="搜索文件"
          aria-expanded={results.length > 0}
          aria-controls="quick-open-results"
          aria-activedescendant={results[activeIdx] ? `qo-${activeIdx}` : undefined}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActiveIdx((i) => Math.max(0, Math.min(i + 1, results.length - 1)));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActiveIdx((i) => Math.max(i - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              if (results[activeIdx]) {
                if (e.shiftKey) mentionFile(results[activeIdx]);
                else openFile(results[activeIdx]);
              }
            }
          }}
          placeholder="搜索文件…"
          style={{
            minWidth: 0,
            flex: 1,
            background: "transparent",
            border: 0,
            padding: "14px 16px",
            color: "var(--text-primary)",
            fontSize: "var(--text-md)",
            outline: 0,
          }}
        />
        <button type="button" className="mc-icon-btn" onClick={close} title="关闭快速打开" aria-label="关闭快速打开" style={{ width: 32, height: 32, flexShrink: 0, marginRight: 8 }}>
          <X size={16} />
        </button>
        </div>
        <div id="quick-open-results" role="listbox" style={{ borderTop: "1px solid var(--border-subtle)", maxHeight: 360, minHeight: 0, overflowY: "auto" }}>
          {storeLoading && (
            <div style={{ padding: 14, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
              正在搜索…
            </div>
          )}
          {!storeLoading && showingOpenTabs && results.length > 0 && (
            <div aria-hidden="true" style={{ padding: "8px 16px 4px", color: "var(--text-muted)", fontSize: "var(--text-2xs)", fontWeight: "var(--fw-semibold)" }}>
              打开的文件
            </div>
          )}
          {!storeLoading && showingOpenTabs && results.length === 0 && (
            <div style={{ padding: 14, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
              输入关键词搜索工作区文件。
            </div>
          )}
          {!storeLoading && !showingOpenTabs && results.length === 0 && (
            <div role={searchError ? "alert" : undefined} style={{ padding: 14, color: searchError ? "var(--state-danger)" : "var(--text-muted)", fontSize: "var(--text-sm)" }}>
              {searchError || "未找到文件。"}
            </div>
          )}
          {results.map((file, i) => (
            <button
              key={file.path}
              id={`qo-${i}`}
              role="option"
              aria-selected={i === activeIdx}
              onClick={() => openFile(file)}
              onMouseEnter={() => setActiveIdx(i)}
              style={{
                width: "100%",
                textAlign: "left",
                padding: "8px 16px",
                background: i === activeIdx ? "var(--surface-active)" : "transparent",
                border: 0,
                cursor: "pointer",
                color: "var(--text-primary)",
                fontSize: "var(--text-sm)",
                display: "flex",
                alignItems: "center",
                gap: 10,
              }}
            >
              <FileText size={14} style={{ color: "var(--text-muted)", flexShrink: 0 }} aria-hidden="true" />
              <span title={file.path} style={{ flex: 1, minWidth: 0 }}>
                <span style={{ display: "block", color: "var(--accent-primary)", fontWeight: "var(--fw-medium)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{file.name}</span>
                <span style={{ display: "block", color: "var(--text-muted)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {file.path}
                </span>
              </span>
            </button>
          ))}
        </div>
        <div
          aria-hidden="true"
          style={{
            display: "flex",
            flexWrap: "wrap",
            flexShrink: 0,
            alignItems: "center",
            gap: 12,
            padding: "8px 16px",
            borderTop: "1px solid var(--border-subtle)",
            color: "var(--text-muted)",
            fontSize: "var(--text-2xs)",
          }}
        >
          <span><kbd className="mc-kbd">↑↓</kbd> 导航</span>
          <span><kbd className="mc-kbd">↵</kbd> 打开</span>
          <span><kbd className="mc-kbd">⇧↵</kbd> 添加到上下文</span>
          <span><kbd className="mc-kbd">Esc</kbd> 关闭</span>
        </div>
      </div>
    </div>
  );
};
