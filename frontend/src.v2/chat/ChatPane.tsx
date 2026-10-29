import { useI18n } from "../i18n";
import { memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { MessageList } from "./MessageList";
import { Composer } from "../composer/Composer";
import { ChatSearch } from "./ChatSearch";
import { SafeBoundary } from "../shell/ChunkErrorBoundary";
import { ComposerErrorFallback } from "../components/ComposerErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";
import { ChatContextCard } from "./ChatContextCard";
import { TurnChangeSummary } from "./TurnChangeSummary";
import { useAppStore } from "../stores";
import { TranscriptSearchContext } from "./TranscriptSearchContext";
import { threadSummaryDisplayMode } from "./thread-summary-layout";
import { selectPendingDraftUserMessage } from "../stores/conversation-drafts";

const ThreadSummary = ({ pane }: { pane: HTMLDivElement }) => {
  const [displayMode, setDisplayMode] = useState(() => threadSummaryDisplayMode(pane.clientWidth));
  useLayoutEffect(() => {
    setDisplayMode(threadSummaryDisplayMode(pane.clientWidth));
    const observer = new ResizeObserver(([entry]) => setDisplayMode(threadSummaryDisplayMode(entry.contentRect.width)));
    observer.observe(pane);
    return () => observer.disconnect();
  }, [pane]);
  return <ChatContextCard displayMode={displayMode} />;
};

export const ChatPane = memo(function ChatPane() {
  const { t: tr } = useI18n();
  const [showSearch, setShowSearch] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [pane, setPane] = useState<HTMLDivElement | null>(null);
  const attachPane = useCallback((element: HTMLDivElement | null) => {
    containerRef.current = element;
    setPane(element);
  }, []);
  const messageContainerRef = useRef<HTMLDivElement>(null);
  const conversationId = useAppStore((state) => state.conversationId);
  const pendingConversationSwitchId = useAppStore((state) => state.pendingConversationSwitchId);
  const floating = useAppStore((state) => state.rightPanelExpanded && state.rightPanelOpen);
  useEffect(() => { if (messageContainerRef.current) messageContainerRef.current.inert = floating; }, [floating]);
  const isHydrating = useAppStore((state) => Boolean(
    conversationId && state.conversationHydration[conversationId]?.isHydrating,
  ));
  const hasMessages = useAppStore((state) => state.messages.length > 0);
  const pendingDraftMessage = useAppStore(selectPendingDraftUserMessage);
  const showLoading = !pendingDraftMessage && (Boolean(pendingConversationSwitchId) || (isHydrating && !hasMessages));

  const handleCloseSearch = useCallback(() => {
    setShowSearch(false);
    window.getSelection()?.removeAllRanges();
  }, []);

  useEffect(() => {
    setShowSearch(false);
  }, [conversationId, pendingConversationSwitchId]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "f" && (e.ctrlKey || e.metaKey)) {
        if (e.target instanceof Node && e.target !== document && e.target !== document.body && !containerRef.current?.contains(e.target)) return;
        const state = useAppStore.getState();
        if (state.appMode === "code" && state.panelSlots.find((slot) => slot.focused)?.kind === "editor") return;
        e.preventDefault();
        e.stopPropagation();
        setShowSearch((current) => !current);
        return;
      }
      if (e.key === "Escape") {
        setShowSearch((current) => {
          if (current) window.getSelection()?.removeAllRanges();
          return false;
        });
      }
    };

    const onRequestSearch = () => setShowSearch(true);

    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("chat:request-search", onRequestSearch);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("chat:request-search", onRequestSearch);
    };
  }, []);

  return (
    <div
      ref={attachPane}
      className="chat-pane flex-1 min-h-0 overflow-hidden w-full"
      data-switching={showLoading ? "true" : "false"}
      aria-busy={showLoading}
      style={{
        position: "relative",
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr)",
        gridTemplateRows: "minmax(0, 1fr)",
        flex: 1,
        minHeight: 0,
        overflow: "hidden",
        width: "100%",
        background: "var(--surface-base)",
      }}
    >
        {showLoading && (
          <div role="status" aria-live="polite" className="chat-pane-switch-status">
            <LoaderCircle size={20} className="animate-spin" aria-hidden="true" />
            <span className="sr-only">{tr("正在打开会话…")}</span>
          </div>
        )}
        <div className="chat-pane-layout" aria-hidden={showLoading || undefined} style={showLoading ? { visibility: "hidden" } : undefined}>
        <div className="chat-pane-main">
          {showSearch && (
            <ChatSearch onClose={handleCloseSearch} containerRef={messageContainerRef} />
          )}
          {isHydrating && hasMessages && !pendingConversationSwitchId && (
            <div
              className="chat-pane-hydration-status"
              role="status"
              aria-live="polite"
              style={{
                position: "absolute",
                width: 1,
                height: 1,
                margin: -1,
                padding: 0,
                overflow: "hidden",
                clip: "rect(0 0 0 0)",
                clipPath: "inset(50%)",
                whiteSpace: "nowrap",
                border: 0,
              }}
            >
              {tr("正在恢复会话上下文、运行状态和工具记录…")}</div>
          )}
          <div ref={messageContainerRef} className="chat-pane-message-transition" data-hydrating={isHydrating ? "true" : "false"}>
            <SafeBoundary fallback={<ChatErrorFallback />}>
              <TranscriptSearchContext.Provider value={showSearch}>
              <MessageList searchActive={showSearch} />
              </TranscriptSearchContext.Provider>
            </SafeBoundary>
          </div>
          <div className="chat-pane-composer-region">
            <TurnChangeSummary />
            <SafeBoundary fallback={<ComposerErrorFallback />}>
              <Composer />
            </SafeBoundary>
          </div>
        </div>
        {pane && <ThreadSummary pane={pane} />}
        </div>
    </div>
  );
});
