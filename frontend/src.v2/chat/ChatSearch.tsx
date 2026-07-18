import { useState, useCallback, useEffect, useRef } from "react";

interface SearchMatch {
  range: Range;
}

interface ChatSearchProps {
  onClose: () => void;
  containerRef: React.RefObject<HTMLElement>;
}

export function ChatSearch({ onClose, containerRef }: ChatSearchProps) {
  const [query, setQuery] = useState("");
  const [matchCount, setMatchCount] = useState(0);
  const [currentIndex, setCurrentIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const matchesRef = useRef<SearchMatch[]>([]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Clear browser selection on unmount
  useEffect(() => {
    return () => {
      window.getSelection()?.removeAllRanges();
    };
  }, []);

  const selectMatch = useCallback((index: number) => {
    const match = matchesRef.current[index];
    if (!match) return;
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(match.range);
    const target = match.range.startContainer.parentElement;
    target?.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    setCurrentIndex(index + 1);
  }, []);

  const collectMatches = useCallback(
    (text: string) => {
      if (!text || !containerRef.current) {
        matchesRef.current = [];
        setMatchCount(0);
        setCurrentIndex(0);
        return;
      }
      const container = containerRef.current;
      const treeWalker = document.createTreeWalker(
        container,
        NodeFilter.SHOW_TEXT,
        {
          acceptNode(node) {
            return node.parentElement?.closest("[aria-hidden='true'], button, input, textarea, [contenteditable='true']")
              ? NodeFilter.FILTER_REJECT
              : NodeFilter.FILTER_ACCEPT;
          },
        },
      );
      const nodes: Text[] = [];
      let combined = "";
      const spans: Array<{ node: Text; start: number; end: number }> = [];
      while (treeWalker.nextNode()) {
        const node = treeWalker.currentNode as Text;
        const value = node.data;
        if (!value) continue;
        nodes.push(node);
        spans.push({ node, start: combined.length, end: combined.length + value.length });
        combined += value;
      }
      const searchLower = text.toLowerCase();
      const combinedLower = combined.toLowerCase();
      const matches: SearchMatch[] = [];
      let start = combinedLower.indexOf(searchLower);
      while (start >= 0) {
        const end = start + text.length;
        const startSpan = spans.find((span) => start >= span.start && start < span.end);
        const endSpan = [...spans].reverse().find((span) => end > span.start && end <= span.end);
        if (startSpan && endSpan) {
          const range = document.createRange();
          range.setStart(startSpan.node, start - startSpan.start);
          range.setEnd(endSpan.node, end - endSpan.start);
          matches.push({ range });
        }
        start = combinedLower.indexOf(searchLower, start + Math.max(1, text.length));
      }
      matchesRef.current = matches;
      setMatchCount(matches.length);
      setCurrentIndex(0);
      window.getSelection()?.removeAllRanges();
      if (matches.length > 0) selectMatch(0);
    },
    [containerRef, selectMatch],
  );

  useEffect(() => {
    collectMatches(query);
  }, [query, collectMatches]);

  const findNext = useCallback(
    (backwards = false) => {
      if (!query || matchCount === 0) return;
      const current = Math.max(0, currentIndex - 1);
      const next = backwards
        ? (current - 1 + matchCount) % matchCount
        : (current + 1) % matchCount;
      selectMatch(next);
    },
    [query, matchCount, currentIndex, selectMatch],
  );

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      findNext(e.shiftKey);
      return;
    }
  };

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "8px",
        padding: "6px 12px",
        background: "var(--surface-2, #2a2a2a)",
        borderBottom: "1px solid var(--border-subtle, #333)",
        flexShrink: 0,
      }}
    >
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
        }}
        onKeyDown={handleKeyDown}
        placeholder="Search in conversation..."
        style={{
          flex: 1,
          padding: "4px 8px",
          background: "var(--surface-base, #1e1e1e)",
          color: "var(--text-primary, #e0e0e0)",
          border: "1px solid var(--border-soft, #444)",
          borderRadius: "4px",
          outline: "none",
          fontSize: "13px",
          fontFamily: "inherit",
        }}
      />
      {query && (
        <span
          style={{
            fontSize: "12px",
            color:
              matchCount > 0
                ? "var(--text-muted, #888)"
                : "var(--text-error, #e55)",
            whiteSpace: "nowrap",
            minWidth: "60px",
            textAlign: "center",
          }}
        >
          {matchCount > 0
            ? `${currentIndex}/${matchCount}`
            : "No matches"}
        </span>
      )}
      <button
        onClick={() => findNext(true)}
        title="Previous (Shift+Enter)"
        style={btnStyle}
        onMouseEnter={(e) =>
          (e.currentTarget.style.background = "var(--surface-3, #3a3a3a)")
        }
        onMouseLeave={(e) =>
          (e.currentTarget.style.background = "transparent")
        }
      >
        &#x2191;
      </button>
      <button
        onClick={() => findNext(false)}
        title="Next (Enter)"
        style={btnStyle}
        onMouseEnter={(e) =>
          (e.currentTarget.style.background = "var(--surface-3, #3a3a3a)")
        }
        onMouseLeave={(e) =>
          (e.currentTarget.style.background = "transparent")
        }
      >
        &#x2193;
      </button>
      <button
        onClick={onClose}
        title="Close (Escape)"
        style={btnStyle}
        onMouseEnter={(e) =>
          (e.currentTarget.style.background = "var(--surface-3, #3a3a3a)")
        }
        onMouseLeave={(e) =>
          (e.currentTarget.style.background = "transparent")
        }
      >
        &#x2715;
      </button>
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "var(--text-secondary, #aaa)",
  cursor: "pointer",
  fontSize: "14px",
  padding: "2px 6px",
  borderRadius: "3px",
  lineHeight: 1,
};
