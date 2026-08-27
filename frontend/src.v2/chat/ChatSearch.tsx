import { useState, useCallback, useEffect, useRef } from "react";
import { ChevronDown, ChevronUp, X } from "lucide-react";

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
      role="search"
      aria-label="在对话中搜索"
      className="chat-pane-search"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "8px",
        padding: "6px 12px",
        background: "var(--surface-page)",
        borderBottom: "1px solid var(--border-subtle)",
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
        placeholder="在对话中搜索…"
        aria-label="搜索对话内容"
        className="chat-search-input"
        style={{
          flex: 1,
          padding: "4px 8px",
          background: "var(--surface-base)",
          color: "var(--text-primary)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-sm, 6px)",
          fontSize: "var(--text-chrome)",
          fontFamily: "inherit",
        }}
      />
      {query && (
        <span
          style={{
            fontSize: "var(--text-xxs)",
            color:
              matchCount > 0
                ? "var(--text-muted)"
                : "var(--state-danger)",
            whiteSpace: "nowrap",
            minWidth: "60px",
            textAlign: "center",
          }}
        >
          {matchCount > 0
            ? `${currentIndex}/${matchCount}`
            : "无匹配项"}
        </span>
      )}
      <button
        type="button"
        onClick={() => findNext(true)}
        title="上一个匹配项（Shift + Enter）"
        aria-label="上一个匹配项"
        style={btnStyle}
        onMouseEnter={(e) =>
          (e.currentTarget.style.background = "var(--surface-hover)")
        }
        onMouseLeave={(e) =>
          (e.currentTarget.style.background = "transparent")
        }
      >
        <ChevronUp size={16} aria-hidden="true" />
      </button>
      <button
        type="button"
        onClick={() => findNext(false)}
        title="下一个匹配项（Enter）"
        aria-label="下一个匹配项"
        style={btnStyle}
        onMouseEnter={(e) =>
          (e.currentTarget.style.background = "var(--surface-hover)")
        }
        onMouseLeave={(e) =>
          (e.currentTarget.style.background = "transparent")
        }
      >
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      <button
        type="button"
        onClick={onClose}
        title="关闭搜索（Escape）"
        aria-label="关闭搜索"
        style={btnStyle}
        onMouseEnter={(e) =>
          (e.currentTarget.style.background = "var(--surface-hover)")
        }
        onMouseLeave={(e) =>
          (e.currentTarget.style.background = "transparent")
        }
      >
        <X size={16} aria-hidden="true" />
      </button>
    </div>
  );
}

const btnStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "var(--text-secondary)",
  cursor: "pointer",
  width: 30,
  height: 30,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
  borderRadius: "var(--radius-sm, 6px)",
  lineHeight: 1,
};
