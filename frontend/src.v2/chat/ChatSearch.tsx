import { useState, useCallback, useEffect, useRef } from "react";
import { ChevronDown, ChevronUp, X } from "lucide-react";
import { useAppStore } from "../stores";
import { loadEarlierConversationMessages, loadEarlierToolItems } from "./historyPagination";

interface SearchMatch {
  range: Range;
}

interface ChatSearchProps {
  onClose: () => void;
  containerRef: React.RefObject<HTMLElement>;
}

export function ChatSearch({ onClose, containerRef }: ChatSearchProps) {
  const conversationId = useAppStore((state) => state.conversationId);
  const historyPage = useAppStore((state) => conversationId ? state.conversationHistoryPages[conversationId] : undefined);
  const unloadedToolMessageId = useAppStore((state) => state.messages.find((message) => (message.toolPage?.remaining ?? 0) > 0)?.id);
  const [loadingTools, setLoadingTools] = useState(false);
  const [query, setQuery] = useState("");
  const [matchCount, setMatchCount] = useState(0);
  const [currentIndex, setCurrentIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const matchesRef = useRef<SearchMatch[]>([]);
  const currentIndexRef = useRef(0);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Clear browser selection on unmount
  useEffect(() => {
    return () => {
      window.getSelection()?.removeAllRanges();
    };
  }, []);

  const selectMatch = useCallback((index: number, scroll = true) => {
    const match = matchesRef.current[index];
    if (!match) return;
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(match.range);
    const target = match.range.startContainer.parentElement;
    if (scroll) target?.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
    currentIndexRef.current = index + 1;
    setCurrentIndex(index + 1);
  }, []);

  const collectMatches = useCallback(
    (text: string, resetSelection = true) => {
      const selectedRange = matchesRef.current[currentIndexRef.current - 1]?.range;
      if (!text || !containerRef.current) {
        matchesRef.current = [];
        currentIndexRef.current = 0;
        setMatchCount(0);
        setCurrentIndex(0);
        window.getSelection()?.removeAllRanges();
        return;
      }
      const container = containerRef.current;
      const treeWalker = document.createTreeWalker(
        container,
        NodeFilter.SHOW_TEXT,
        {
          acceptNode(node) {
            return node.parentElement?.closest("[hidden], [aria-hidden='true'], input, textarea, [contenteditable='true'], .cell-action-btn, .exec-cell-stop-button")
              ? NodeFilter.FILTER_REJECT
              : NodeFilter.FILTER_ACCEPT;
          },
        },
      );
      let combined = "";
      let previousBlock: Element | null | undefined;
      const spans: Array<{ node: Text; start: number; end: number }> = [];
      while (treeWalker.nextNode()) {
        const node = treeWalker.currentNode as Text;
        const value = node.data;
        if (!value) continue;
        const block = node.parentElement?.closest(
          "p, li, pre, blockquote, h1, h2, h3, h4, h5, h6, td, th, .user-cell-bubble, .assistant-cell-content, .agent-loop-turn",
        );
        if (spans.length > 0 && block !== previousBlock) combined += "\n";
        previousBlock = block;
        spans.push({ node, start: combined.length, end: combined.length + value.length });
        combined += value;
      }
      // Case folding can change UTF-16 length (for example İ). Regex matches
      // retain original offsets, which are the offsets DOM Range requires.
      const pattern = new RegExp(text.replace(/[.*+?^\u0024{}()|[\]\\]/g, "\\$&"), "giu");
      const matches: SearchMatch[] = [];
      let spanIndex = 0;
      for (const match of combined.matchAll(pattern)) {
        const start = match.index;
        const end = start + match[0].length;
        while (spanIndex < spans.length && start >= spans[spanIndex].end) spanIndex += 1;
        const startSpan = spans[spanIndex];
        let endSpanIndex = spanIndex;
        while (endSpanIndex < spans.length && end > spans[endSpanIndex].end) endSpanIndex += 1;
        const endSpan = spans[endSpanIndex];
        if (startSpan && endSpan && start >= startSpan.start && end > endSpan.start) {
          const range = document.createRange();
          range.setStart(startSpan.node, start - startSpan.start);
          range.setEnd(endSpan.node, end - endSpan.start);
          matches.push({ range });
        }
        spanIndex = endSpanIndex;
      }
      const selectedIndex = !resetSelection && selectedRange
        ? matches.findIndex(({ range }) => range.startContainer === selectedRange.startContainer
          && range.startOffset === selectedRange.startOffset)
        : -1;
      const nextIndex = selectedIndex >= 0 ? selectedIndex
        : Math.min(Math.max(0, resetSelection ? 0 : currentIndexRef.current - 1), matches.length - 1);
      matchesRef.current = matches;
      setMatchCount(matches.length);
      if (matches.length > 0) selectMatch(nextIndex, resetSelection);
      else {
        currentIndexRef.current = 0;
        setCurrentIndex(0);
        window.getSelection()?.removeAllRanges();
      }
    },
    [containerRef, selectMatch],
  );

  useEffect(() => {
    collectMatches(query);
    const container = containerRef.current;
    if (!container || !query) return;
    // The ref owns transcript DOM only, so updating the match label/selection
    // cannot feed this observer. Streams and history hydration update it.
    const observer = new MutationObserver(() => collectMatches(query, false));
    observer.observe(container, { childList: true, characterData: true, subtree: true });
    return () => observer.disconnect();
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
        flexWrap: "wrap",
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
        disabled={matchCount === 0}
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
        disabled={matchCount === 0}
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
      <div className="chat-search-scope" style={{ flexBasis: "100%", display: "flex", justifyContent: "space-between", gap: 10, fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>
        <span>搜索已加载的正文与工具记录{historyPage?.hasMore || unloadedToolMessageId ? " · 还有更早历史未检索" : ""}</span>
        {historyPage?.hasMore && conversationId && <button type="button" disabled={historyPage.loading}
          onClick={() => void loadEarlierConversationMessages(conversationId)}
          style={{ border: 0, background: "transparent", color: "var(--accent-primary)", padding: 0, whiteSpace: "nowrap" }}>
          {historyPage.loading ? "正在加载…" : "载入更早历史继续搜索"}
        </button>}
        {unloadedToolMessageId && conversationId && <button type="button" disabled={loadingTools} onClick={() => {
          setLoadingTools(true);
          void loadEarlierToolItems(conversationId, unloadedToolMessageId).finally(() => setLoadingTools(false));
        }} style={{ border: 0, background: "transparent", color: "var(--accent-primary)", padding: 0, whiteSpace: "nowrap" }}>{loadingTools ? "正在加载…" : "载入更早工具记录"}</button>}
      </div>
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
