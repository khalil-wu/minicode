import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { isDesktop, fsSearchFiles } from "../desktop/runtime";
import { searchWorkspaceFiles } from "../protocol/workspace";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { useFocusTrap } from "../hooks/useFocusTrap";

export const QuickOpen = () => {
  const visible = useAppStore((s) => s.quickOpenVisible);
  const storeResults = useAppStore((s) => s.quickOpenResults);
  const storeLoading = useAppStore((s) => s.quickOpenLoading);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const enabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [query, setQuery] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<number>(0);
  const dialogRef = useFocusTrap(visible && enabled);

  useEffect(() => {
    if (visible && !enabled) {
      useAppStore.setState({ quickOpenVisible: false, quickOpenResults: [], quickOpenLoading: false });
      return;
    }
    if (visible) {
      setQuery("");
      setActiveIdx(0);
      useAppStore.setState({ quickOpenResults: [], quickOpenLoading: false });
    }
  }, [visible, enabled]);

  const close = () => useAppStore.setState({ quickOpenVisible: false });

  const search = (q: string) => {
    setQuery(q);
    setActiveIdx(0);
    if (!q.trim()) {
      useAppStore.setState({ quickOpenResults: [], quickOpenLoading: false });
      return;
    }
    clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      useAppStore.setState({ quickOpenLoading: true });
      if (isDesktop()) {
        const workingDirectory = useAppStore.getState().workingDirectory || "";
        fsSearchFiles(workingDirectory, q, 20).then((results) => {
          useAppStore.setState({ quickOpenResults: results, quickOpenLoading: false });
        }).catch(() => {
          useAppStore.setState({ quickOpenResults: [], quickOpenLoading: false });
        });
      } else {
        const workingDirectory = useAppStore.getState().workingDirectory || "";
        searchWorkspaceFiles(workingDirectory, q, 20).then((results) => {
          useAppStore.setState({ quickOpenResults: results, quickOpenLoading: false });
        }).catch(() => {
          useAppStore.setState({ quickOpenResults: [], quickOpenLoading: false });
        });
      }
    }, 200);
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
        role="dialog"
        aria-modal="true"
        aria-label="Quick open file"
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
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-md, 12px)",
          boxShadow: "var(--shadow-strong, var(--shadow-md))",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          pointerEvents: "auto",
        }}
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => search(e.target.value)}
          role="combobox"
          aria-expanded={storeResults.length > 0}
          aria-controls="quick-open-results"
          aria-activedescendant={storeResults[activeIdx] ? `qo-${activeIdx}` : undefined}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActiveIdx((i) => Math.min(i + 1, storeResults.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActiveIdx((i) => Math.max(i - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              if (storeResults[activeIdx]) {
                if (e.shiftKey) mentionFile(storeResults[activeIdx]);
                else openFile(storeResults[activeIdx]);
              }
            }
          }}
          placeholder="Search files... (Enter to open, Shift+Enter to @mention)"
          style={{
            background: "transparent",
            border: 0,
            padding: "14px 16px",
            color: "var(--text-primary)",
            fontSize: "var(--text-md)",
            outline: 0,
          }}
        />
        <div id="quick-open-results" role="listbox" style={{ borderTop: "1px solid var(--border-subtle)", maxHeight: 360, overflowY: "auto" }}>
          {storeLoading && (
            <div style={{ padding: 14, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
              Searching...
            </div>
          )}
          {!storeLoading && query && storeResults.length === 0 && (
            <div style={{ padding: 14, color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
              No files found.
            </div>
          )}
          {storeResults.map((file, i) => (
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
              <span style={{ color: "var(--accent-primary)", fontWeight: 500 }}>{file.name}</span>
              <span style={{ flex: 1, color: "var(--text-muted)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {file.path}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};
