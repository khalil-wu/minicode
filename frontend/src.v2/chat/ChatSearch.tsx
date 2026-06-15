import { useState, useCallback, useEffect, useRef } from "react";

// window.find() is a non-standard but widely supported browser API
declare global {
  interface Window {
    find(
      text?: string,
      caseSensitive?: boolean,
      backwards?: boolean,
      wrapAround?: boolean,
    ): boolean;
  }
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

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Clear browser selection on unmount
  useEffect(() => {
    return () => {
      window.getSelection()?.removeAllRanges();
    };
  }, []);

  // Count matches by walking text nodes in the container
  const countMatches = useCallback(
    (text: string) => {
      if (!text || !containerRef.current) {
        setMatchCount(0);
        setCurrentIndex(0);
        return;
      }
      const container = containerRef.current;
      const treeWalker = document.createTreeWalker(
        container,
        NodeFilter.SHOW_TEXT,
        null,
      );
      let count = 0;
      const searchLower = text.toLowerCase();
      while (treeWalker.nextNode()) {
        const nodeText = treeWalker.currentNode.textContent || "";
        const lower = nodeText.toLowerCase();
        let idx = lower.indexOf(searchLower);
        while (idx !== -1) {
          count++;
          idx = lower.indexOf(searchLower, idx + 1);
        }
      }
      setMatchCount(count);
      if (count > 0) {
        setCurrentIndex(1);
      } else {
        setCurrentIndex(0);
      }
    },
    [containerRef],
  );

  useEffect(() => {
    countMatches(query);
  }, [query, countMatches]);

  const findNext = useCallback(
    (backwards = false) => {
      if (!query || matchCount === 0) return;
      const found = window.find(query, false, backwards, true);
      if (found) {
        setCurrentIndex((prev) => {
          if (backwards) return Math.max(1, prev - 1);
          return Math.min(matchCount, prev + 1);
        });
      } else {
        // Wrap around: search from the opposite direction
        window.find(query, false, !backwards, true);
        setCurrentIndex(backwards ? matchCount : 1);
      }
    },
    [query, matchCount],
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
