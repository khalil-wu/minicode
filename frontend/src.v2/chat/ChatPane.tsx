import { useCallback, useEffect, useRef, useState } from "react";
import { MessageList } from "./MessageList";
import { Composer } from "../composer/Composer";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { ChatSearch } from "./ChatSearch";
import { SafeBoundary } from "../shell/ChunkErrorBoundary";
import { ComposerErrorFallback } from "../components/ComposerErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";
import { TaskSuggestions } from "./components/TaskSuggestions";  // 🔧 新增

export const ChatPane = () => {
  const [showSearch, setShowSearch] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleCloseSearch = useCallback(() => {
    setShowSearch(false);
    window.getSelection()?.removeAllRanges();
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "f" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        e.stopPropagation();
        setShowSearch(true);
      }
    };

    const onRequestSearch = () => setShowSearch(true);

    el.addEventListener("keydown", onKeyDown);
    window.addEventListener("chat:request-search", onRequestSearch);
    return () => {
      el.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("chat:request-search", onRequestSearch);
    };
  }, []);

  return (
    <div
      ref={containerRef}
      className="flex flex-1 min-h-0 flex-col overflow-hidden w-full"
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
      <InlineAgentPrompt />
      <TaskSuggestions />  {/* 🔧 新增：AI 任务建议 */}
      <SafeBoundary fallback={<ComposerErrorFallback />}>
        <Composer />
      </SafeBoundary>
    </div>
  );
};
