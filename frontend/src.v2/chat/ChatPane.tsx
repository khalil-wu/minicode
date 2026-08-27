import { useCallback, useEffect, useRef, useState } from "react";
import { MessageList } from "./MessageList";
import { Composer } from "../composer/Composer";
import { ChatSearch } from "./ChatSearch";
import { SafeBoundary } from "../shell/ChunkErrorBoundary";
import { ComposerErrorFallback } from "../components/ComposerErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";
import { ChatContextCard } from "./ChatContextCard";
import { useAppStore } from "../stores";

export const ChatPane = () => {
  const [showSearch, setShowSearch] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const conversationId = useAppStore((state) => state.conversationId);
  const isHydrating = useAppStore((state) => Boolean(
    conversationId && state.conversationHydration[conversationId]?.isHydrating,
  ));

  const handleCloseSearch = useCallback(() => {
    setShowSearch(false);
    window.getSelection()?.removeAllRanges();
  }, []);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() === "f" && (e.ctrlKey || e.metaKey)) {
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
      ref={containerRef}
      className="chat-pane flex-1 min-h-0 overflow-hidden w-full"
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
      <div className="chat-pane-layout">
        <div className="chat-pane-main">
          {showSearch && (
            <ChatSearch onClose={handleCloseSearch} containerRef={containerRef} />
          )}
          {isHydrating && (
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
              正在恢复会话上下文、运行状态和工具记录…
            </div>
          )}
          <div className="chat-pane-message-transition" data-hydrating={isHydrating ? "true" : "false"}>
            <SafeBoundary fallback={<ChatErrorFallback />}>
              <MessageList />
            </SafeBoundary>
          </div>
          <div className="chat-pane-composer-region">
            <SafeBoundary fallback={<ComposerErrorFallback />}>
              <Composer />
            </SafeBoundary>
          </div>
        </div>
        <ChatContextCard />
      </div>
    </div>
  );
};
