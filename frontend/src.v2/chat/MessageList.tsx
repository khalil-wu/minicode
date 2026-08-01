import { useCallback, useDeferredValue, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown } from "lucide-react";
import { useAppStore } from "../stores";
import { BrandMark } from "../components/icons";
import { projectMessagesToTurns, projectRecentMessagesToTurns } from "./chatSurfaceState";
import { ChatTurn } from "./components/ChatTurn";
import { hasVisibleActiveConversation } from "./activeConversation";

const RECENT_TURN_WINDOW = 40;
const MAX_TURNS_WITHOUT_VIRTUALIZATION = 8;
const ESTIMATED_TURN_HEIGHT = 220;
const ESTIMATED_TURN_GAP = 8;
const INITIAL_VIRTUAL_VIEWPORT_HEIGHT = 800;
const estimateTurnHeight = () => ESTIMATED_TURN_HEIGHT;

export const MessageList = () => {
  const messages = useAppStore((s) => s.messages);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const conversationId = useAppStore((s) => s.conversationId);
  const conversations = useAppStore((s) => s.conversations);
  const liveGitChanges = useAppStore((s) => s.gitChanges.live);

  // Deferred messages: during streaming, use the live messages so the
  // streaming tail updates in real-time. When NOT streaming, defer the
  // messages to keep the composer input responsive — the deferred value
  // lags behind by one frame but the user isn't watching for updates
  // at that point. Mirrors cc's usesSyncMessages approach (REPL.tsx:4560).
  const deferredMessages = useDeferredValue(messages);
  const displayMessages = isStreaming ? messages : deferredMessages;
  const timelineMessages = useMemo(
    () => displayMessages.filter((message) => message.queueState !== "queued"),
    [displayMessages],
  );
  const ref = useRef<HTMLDivElement>(null);
  const getScrollElement = useCallback(() => ref.current, []);
  const contentRef = useRef<HTMLDivElement>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [isFollowing, setIsFollowing] = useState(true);
  const isNearBottom = useRef(true);
  const prevConvId = useRef(conversationId);
  const lastRenderedMessageIdRef = useRef<string | null>(messages.at(-1)?.id ?? null);
  const lastRenderedMessageCountRef = useRef(messages.length);
  const userScrollIntentRef = useRef(0);
  const [faded, setFaded] = useState(false);
  const [showAllHistory, setShowAllHistory] = useState(false);
  const [isScrollbarActive, setIsScrollbarActive] = useState(false);
  const scrollbarFadeTimerRef = useRef<number | null>(null);

  const shouldShowMessages = hasVisibleActiveConversation(conversationId, conversations)
    || (isStreaming && messages.length > 0);

  const projectedTurns = useMemo(
    () => !shouldShowMessages
      ? { turns: [], hiddenTurnCount: 0, totalTurnCount: 0 }
      : showAllHistory
        ? {
            turns: projectMessagesToTurns(timelineMessages, isStreaming),
            hiddenTurnCount: 0,
            totalTurnCount: 0,
          }
        : projectRecentMessagesToTurns(timelineMessages, isStreaming, RECENT_TURN_WINDOW),
    [isStreaming, showAllHistory, shouldShowMessages, timelineMessages],
  );
  const turns = projectedTurns.turns;
  const hiddenTurnCount = projectedTurns.hiddenTurnCount;
  // Keep the newest turn outside historical virtualization. The current reply
  // then streams without invalidating row measurements, and a newly restored
  // conversation always paints its latest result before observers settle.
  const tailTurn = turns.at(-1) ?? null;
  const liveTurn = isStreaming && tailTurn?.status === "streaming" ? tailTurn : null;
  const historicalTurns = tailTurn ? turns.slice(0, -1) : turns;
  const shouldVirtualize = historicalTurns.length > MAX_TURNS_WITHOUT_VIRTUALIZATION;
  const firstHistoricalTurnId = historicalTurns[0]?.id;
  const lastHistoricalTurnId = historicalTurns.at(-1)?.id;
  // TanStack includes getItemKey identity in its measurement cache key. Keep
  // that callback stable while only the streaming tail changes; otherwise a
  // long transcript rebuilds every historical row measurement on each delta.
  const getHistoricalTurnKey = useMemo(() => {
    const keys = historicalTurns.map((turn) => turn.id);
    return (index: number) => keys[index] ?? index;
  }, [conversationId, firstHistoricalTurnId, historicalTurns.length, lastHistoricalTurnId]);
  const initialVirtualOffset = useCallback(() => Math.max(
    0,
    historicalTurns.length * (ESTIMATED_TURN_HEIGHT + ESTIMATED_TURN_GAP)
      - INITIAL_VIRTUAL_VIEWPORT_HEIGHT,
  ), [firstHistoricalTurnId, historicalTurns.length, lastHistoricalTurnId]);
  const virtualListRef = useRef<HTMLDivElement>(null);
  const virtualConversationRef = useRef(conversationId);
  const [virtualScrollMargin, setVirtualScrollMargin] = useState(0);
  useLayoutEffect(() => {
    if (!shouldVirtualize) {
      setVirtualScrollMargin((current) => current === 0 ? current : 0);
      return;
    }
    const list = virtualListRef.current;
    const scrollElement = ref.current;
    if (!list || !scrollElement) return;
    const listRect = list.getBoundingClientRect();
    const scrollRect = scrollElement.getBoundingClientRect();
    const next = Math.max(0, Math.round(listRect.top - scrollRect.top + scrollElement.scrollTop));
    setVirtualScrollMargin((current) => current === next ? current : next);
  }, [hiddenTurnCount, shouldVirtualize]);
  const turnVirtualizer = useVirtualizer({
    enabled: shouldVirtualize,
    count: shouldVirtualize ? historicalTurns.length : 0,
    getScrollElement,
    estimateSize: estimateTurnHeight,
    getItemKey: getHistoricalTurnKey,
    overscan: 3,
    gap: ESTIMATED_TURN_GAP,
    scrollMargin: virtualScrollMargin,
    initialRect: { width: 0, height: INITIAL_VIRTUAL_VIEWPORT_HEIGHT },
    initialOffset: initialVirtualOffset,
  });
  useLayoutEffect(() => {
    if (virtualConversationRef.current === conversationId) return;
    virtualConversationRef.current = conversationId;
    // The virtualizer instance survives conversation switches. Its internal
    // size map is keyed by turn id, so discard measurements owned by the old
    // transcript instead of retaining them across the inactive-chat LRU.
    turnVirtualizer.measure();
  }, [conversationId, turnVirtualizer]);
  const liveScrollSignature = useMemo(() => {
    if (!shouldShowMessages || timelineMessages.length === 0) return "empty";
    const last = timelineMessages[timelineMessages.length - 1];
    const blocks = last?.blocks ?? [];
    const lastBlockRaw = blocks[blocks.length - 1];
    const lastBlock = (lastBlockRaw && typeof lastBlockRaw === "object")
      ? lastBlockRaw as unknown as Record<string, unknown>
      : undefined;
    const record = (lastBlock?.record && typeof lastBlock.record === "object")
      ? lastBlock.record as unknown as Record<string, unknown>
      : undefined;
    return [
      conversationId || "",
      timelineMessages.length,
      last?.id || "",
      last?.role || "",
      last?.content?.length || 0,
      Boolean(last?.isStreaming),
      Boolean(last?.isThinkingStreaming),
      blocks.length,
      String(lastBlock?.type || ""),
      String(lastBlock?.content || "").length,
      String(lastBlock?.status || record?.status || ""),
      String(record?.outputPreview || "").length,
      String(record?.stdoutPreview || "").length,
      String(record?.stderrPreview || "").length,
      turns.length,
      hiddenTurnCount,
    ].join("|");
  }, [conversationId, hiddenTurnCount, shouldShowMessages, timelineMessages, turns.length]);

  const scrollRafRef = useRef<number | null>(null);
  const programmaticScrollRef = useRef(false);
  const lastScrollTopRef = useRef(0);

  const setScrollTop = useCallback((el: HTMLDivElement, top: number, behavior: ScrollBehavior = "auto") => {
    programmaticScrollRef.current = true;
    if (behavior === "smooth") el.scrollTo({ top, behavior });
    else el.scrollTop = top;
    requestAnimationFrame(() => {
      programmaticScrollRef.current = false;
      lastScrollTopRef.current = el.scrollTop;
    });
  }, []);

  const scheduleBottomScroll = useCallback((behavior: ScrollBehavior = "auto", force = false) => {
    if (!force && !isNearBottom.current) return;
    if (force) {
      isNearBottom.current = true;
      setIsFollowing(true);
      setShowScrollBtn(false);
    }
    if (scrollRafRef.current !== null) {
      if (!force) return;
      cancelAnimationFrame(scrollRafRef.current);
      scrollRafRef.current = null;
    }
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = null;
      const el = ref.current;
      if (!el || (!force && !isNearBottom.current)) return;
      if (force) {
        isNearBottom.current = true;
        setIsFollowing(true);
        setShowScrollBtn(false);
      }
      // Mark this as programmatic so the resulting scroll event doesn't
      // re-engage auto-follow or fight a user reading upstream. One snap per
      // frame is enough — the old second-rAF re-snap caused a visible jog.
      const top = el.scrollHeight;
      setScrollTop(el, top, behavior);
    });
  }, [setScrollTop]);
  const followBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    scheduleBottomScroll(behavior, false);
  }, [scheduleBottomScroll]);
  const forceBottom = useCallback((behavior: ScrollBehavior = "auto") => {
    scheduleBottomScroll(behavior, true);
  }, [scheduleBottomScroll]);

  useLayoutEffect(() => {
    if (prevConvId.current !== conversationId) {
      prevConvId.current = conversationId;
      setFaded(true);
      setShowAllHistory(false);
      isNearBottom.current = true;
      setIsFollowing(true);
      setShowScrollBtn(false);
      lastRenderedMessageIdRef.current = messages.at(-1)?.id ?? null;
      lastRenderedMessageCountRef.current = messages.length;
    }
  }, [conversationId, messages]);

  useEffect(() => {
    if (faded) {
      const t = setTimeout(() => {
        setFaded(false);
        const el = ref.current;
        if (el) setScrollTop(el, el.scrollHeight);
      }, 60);
      return () => clearTimeout(t);
    }
  }, [faded, setScrollTop]);

  const scrollToBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    isNearBottom.current = true;
    setIsFollowing(true);
    setShowScrollBtn(false);
    setScrollTop(el, el.scrollHeight, "smooth");
  }, [setScrollTop]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const showScrollbarBriefly = () => {
      setIsScrollbarActive(true);
      if (scrollbarFadeTimerRef.current !== null) window.clearTimeout(scrollbarFadeTimerRef.current);
      scrollbarFadeTimerRef.current = window.setTimeout(() => {
        scrollbarFadeTimerRef.current = null;
        setIsScrollbarActive(false);
      }, 700);
    };
    const onScroll = () => {
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      const prev = lastScrollTopRef.current;
      lastScrollTopRef.current = el.scrollTop;
      // Ignore the scroll event fired by our own follow snap.
      if (programmaticScrollRef.current) {
        return;
      }
      showScrollbarBriefly();
      // Any user-driven upward scroll (wheel OR drag) disengages auto-follow —
      // the old 80px band yanked users back who scrolled up < 80px to read.
      if (el.scrollTop < prev - 4) {
        userScrollIntentRef.current = Date.now();
        isNearBottom.current = false;
        setIsFollowing(false);
        setShowScrollBtn(gap > 200);
        return;
      }
      if (Date.now() - userScrollIntentRef.current < 200) return;
      isNearBottom.current = gap < 80;
      setIsFollowing(gap < 80);
      setShowScrollBtn(gap > 200);
    };
    const onWheel = (event: WheelEvent) => {
      showScrollbarBriefly();
      if (event.deltaY < 0) {
        userScrollIntentRef.current = Date.now();
        isNearBottom.current = false;
        setIsFollowing(false);
      }
    };
    el.addEventListener("scroll", onScroll);
    el.addEventListener("wheel", onWheel, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onWheel);
      if (scrollbarFadeTimerRef.current !== null) window.clearTimeout(scrollbarFadeTimerRef.current);
    };
  }, []);

  useLayoutEffect(() => {
    followBottom();
  }, [followBottom, isStreaming, liveScrollSignature]);

  useLayoutEffect(() => {
    if (!shouldShowMessages) {
      lastRenderedMessageIdRef.current = messages.at(-1)?.id ?? null;
      lastRenderedMessageCountRef.current = messages.length;
      return;
    }
    const previousCount = lastRenderedMessageCountRef.current;
    const appendedMessages = messages.length > previousCount
      ? messages.slice(previousCount)
      : [];
    lastRenderedMessageCountRef.current = messages.length;
    const latest = messages.at(-1);
    const latestId = latest?.id ?? null;
    const messageChanged = Boolean(latestId && latestId !== lastRenderedMessageIdRef.current);
    lastRenderedMessageIdRef.current = latestId;
    if (!messageChanged && appendedMessages.length === 0) return;
    if (appendedMessages.some((message) => message.role === "user")) forceBottom();
    else if (latest?.role === "assistant") followBottom();
  }, [followBottom, forceBottom, messages, shouldShowMessages]);

  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      followBottom();
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [followBottom]);

  useEffect(() => {
    return () => {
      if (scrollRafRef.current !== null) cancelAnimationFrame(scrollRafRef.current);
    };
  }, []);

  return (
    <div
      data-testid="message-list-shell"
      className="relative flex-1 min-h-0 h-full flex flex-col"
      style={{ position: "relative", flex: 1, minHeight: 0, height: "100%", display: "flex", flexDirection: "column" }}
    >
      <div
        ref={ref}
        data-testid="message-list-scroll"
        className={`message-list-scroll chat-scrollbar-auto-hide${isScrollbarActive ? " is-scrolling" : ""} flex-1 min-h-0 overflow-y-auto flex flex-col transition-opacity duration-[120ms] ease-out`}
        role="log"
        aria-label="会话历史"
        tabIndex={0}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-turn-gap)",
          overscrollBehavior: "contain",
          scrollbarGutter: "stable",
          opacity: faded ? 0 : 1,
        }}
      >
        <div
          ref={contentRef}
          className="message-list-content flex flex-col"
          data-layout-mode="code"
          style={{
            gap: "var(--space-turn-gap)",
            minHeight: "100%",
          }}
        >
          {!shouldShowMessages || timelineMessages.length === 0 ? (
            <EmptyState />
          ) : (
            <>
            {hiddenTurnCount > 0 && (
              <button
                type="button"
                onClick={() => setShowAllHistory(true)}
                className="self-center border rounded-md px-3 py-[7px] cursor-pointer font-[650]"
                style={{
                  borderColor: "var(--border-subtle)",
                  borderRadius: "var(--radius-sm, 6px)",
                  background: "var(--surface-page)",
                  color: "var(--text-secondary)",
                  fontFamily: "var(--font-ui)",
                  fontSize: "var(--text-xs)",
                }}
              >
                Show earlier messages ({hiddenTurnCount})
              </button>
            )}
            {shouldVirtualize ? (
              <div
                ref={virtualListRef}
                data-testid="virtual-turn-list"
                style={{
                  flex: "0 0 auto",
                  height: turnVirtualizer.getTotalSize(),
                  position: "relative",
                  width: "100%",
                }}
              >
                {turnVirtualizer.getVirtualItems().map((virtualRow) => {
                  const turn = historicalTurns[virtualRow.index];
                  if (!turn) return null;
                  return (
                    <div
                      key={turn.id}
                      ref={turnVirtualizer.measureElement}
                      data-index={virtualRow.index}
                      style={{
                        left: 0,
                        position: "absolute",
                        top: 0,
                        transform: `translateY(${virtualRow.start - virtualScrollMargin}px)`,
                        width: "100%",
                      }}
                    >
                      <ChatTurn turn={turn} />
                    </div>
                  );
                })}
              </div>
            ) : historicalTurns.map((turn, i) => {
              // Aggressive content-visibility optimization:
              // - Last 5 turns: always visible (active conversation)
              // - Earlier turns: use content-visibility auto for lazy rendering
              const isRecent = i >= turns.length - 5;
              const isVeryOld = i < turns.length - 20;

              return (
                <div
                  key={turn.id}
                  className={
                    i === turns.length - 1
                      ? "anim-message-appear"
                      : i >= turns.length - 3
                        ? "message-enter"
                        : undefined
                  }
                  style={{
                    contentVisibility: isRecent ? "visible" : "auto",
                    containIntrinsicSize: isVeryOld ? "auto 200px" : "auto 120px",
                  }}
                >
                <ChatTurn turn={turn} />
                </div>
              );
            })}
            {tailTurn && (
              <div
                className={liveTurn ? "anim-message-appear" : undefined}
                data-testid={liveTurn ? "streaming-turn-tail" : "latest-turn-tail"}
                style={{ flex: "0 0 auto" }}
              >
                <ChatTurn
                  turn={tailTurn}
                  liveGitChanges={liveTurn ? liveGitChanges : undefined}
                />
              </div>
            )}
            </>
          )}
        </div>
      </div>
      {showScrollBtn && (
        <button
          type="button"
          onClick={scrollToBottom}
          aria-label="回到底部"
          title={isStreaming && !isFollowing ? "回到底部 · 正在回复" : "回到底部"}
          data-testid="scroll-to-bottom-button"
          className="absolute bottom-3 z-[20] inline-flex items-center justify-center cursor-pointer"
          style={{
            left: "50%",
            transform: "translateX(-50%)",
            background: "var(--surface-raised)",
            border: "1px solid var(--border-soft)",
            borderRadius: "var(--radius-full)",
            width: 44,
            minWidth: 44,
            minHeight: 44,
            padding: 0,
            fontSize: "var(--text-xs)",
            color: "var(--text-secondary)",
            boxShadow: "var(--shadow-md)",
            pointerEvents: "auto",
          }}
        >
          <ArrowDown size={18} aria-hidden="true" />
        </button>
      )}
    </div>
  );
};

const EmptyState = () => {
  return (
    <div className="workbench-empty-state">
      <section className="workbench-empty-hero">
        <div className="workbench-empty-brand">
          <div className="workbench-empty-mark" aria-hidden="true">
            <BrandMark size={22} />
          </div>
          <h1 className="workbench-empty-title">我们应该在 MiniCode 中构建什么？</h1>
        </div>
      </section>
    </div>
  );
};
