import { useCallback, useEffect, useRef, useState } from "react";
import { MessageList } from "./MessageList";
import { Composer } from "../composer/Composer";
import { ChatSearch } from "./ChatSearch";
import { SafeBoundary } from "../shell/ChunkErrorBoundary";
import { ComposerErrorFallback } from "../components/ComposerErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";

export const ChatPane = () => {
  const [showSearch, setShowSearch] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

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
      className="chat-pane flex flex-1 min-h-0 flex-col overflow-hidden w-full"
      style={{
        position: "relative",
        display: "flex",
        flex: 1,
        minHeight: 0,
        flexDirection: "column",
        overflow: "hidden",
        width: "100%",
        background: "var(--surface-base)",
      }}
    >
      {showSearch && (
        <ChatSearch onClose={handleCloseSearch} containerRef={containerRef} />
      )}
      <SafeBoundary fallback={<ChatErrorFallback />}>
        <MessageList />
      </SafeBoundary>
      <SafeBoundary fallback={<ComposerErrorFallback />}>
        <Composer />
      </SafeBoundary>
    </div>
  );
};
