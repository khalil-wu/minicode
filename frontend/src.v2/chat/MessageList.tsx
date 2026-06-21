import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Bot, FolderOpen, MessageSquareText, Settings2 } from "lucide-react";
import { useAppStore } from "../stores";
import { formatModelLabel } from "../lib/model-label";
import { workspaceDisplayName } from "../lib/workspace-display";
import { projectMessagesToTurns, projectRecentMessagesToTurns } from "./chatSurfaceState";
import { ChatTurn } from "./components/ChatTurn";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { hasVisibleActiveConversation } from "./activeConversation";

const RECENT_TURN_WINDOW = 40;

export const MessageList = () => {
  const messages = useAppStore((s) => s.messages);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const conversationId = useAppStore((s) => s.conversationId);
  const conversations = useAppStore((s) => s.conversations);
  const appMode = useAppStore((s) => s.appMode);
  const ref = useRef<HTMLDivElement>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [isFollowing, setIsFollowing] = useState(true);
  const isNearBottom = useRef(true);
  const prevConvId = useRef(conversationId);
  const userScrollIntentRef = useRef(0);
  const [faded, setFaded] = useState(false);
  const [showAllHistory, setShowAllHistory] = useState(false);

  const shouldShowMessages = hasVisibleActiveConversation(conversationId, conversations);

  const projectedTurns = useMemo(
    () => !shouldShowMessages
      ? { turns: [], hiddenTurnCount: 0, totalTurnCount: 0 }
      : showAllHistory
        ? {
            turns: projectMessagesToTurns(messages, isStreaming),
            hiddenTurnCount: 0,
            totalTurnCount: 0,
          }
        : projectRecentMessagesToTurns(messages, isStreaming, RECENT_TURN_WINDOW),
    [messages, isStreaming, showAllHistory, shouldShowMessages],
  );
  const turns = projectedTurns.turns;
  const hiddenTurnCount = projectedTurns.hiddenTurnCount;

  useLayoutEffect(() => {
    if (prevConvId.current !== conversationId) {
      prevConvId.current = conversationId;
      setFaded(true);
      setShowAllHistory(false);
      isNearBottom.current = true;
      setIsFollowing(true);
      setShowScrollBtn(false);
    }
  }, [conversationId]);

  useEffect(() => {
    if (faded) {
      const t = setTimeout(() => {
        setFaded(false);
        const el = ref.current;
        if (el) el.scrollTop = el.scrollHeight;
      }, 60);
      return () => clearTimeout(t);
    }
  }, [faded]);

  const scrollToBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    isNearBottom.current = true;
    setIsFollowing(true);
    setShowScrollBtn(false);
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      if (Date.now() - userScrollIntentRef.current < 200) return;
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      isNearBottom.current = gap < 80;
      setIsFollowing(gap < 80);
      setShowScrollBtn(gap > 200);
    };
    const onWheel = (event: WheelEvent) => {
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
    };
  }, []);

  const scrollRafRef = useRef<number | null>(null);
  useEffect(() => {
    if (isNearBottom.current) {
      if (scrollRafRef.current === null) {
        scrollRafRef.current = requestAnimationFrame(() => {
          scrollRafRef.current = null;
          const el = ref.current;
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    }
  }, [messages, isStreaming]);

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
        className="message-list-scroll flex-1 min-h-0 overflow-y-auto flex flex-col px-7 pt-[58px] transition-opacity duration-[120ms] ease-out"
        role="log"
        aria-label="Conversation history"
        tabIndex={0}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-turn-gap)",
          padding: "58px clamp(22px, 7vw, 104px)",
          paddingBottom: appMode === "code" ? 56 : 34,
          overscrollBehavior: "contain",
          scrollbarGutter: "stable",
          background: "var(--surface-base)",
          opacity: faded ? 0 : 1,
        }}
      >
        {!shouldShowMessages || messages.length === 0 ? (
          <>
            <EmptyState />
          </>
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
            {turns.map((turn, i) => {
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
                <ChatTurn turn={turn} wide={appMode === "code"} />
                </div>
              );
            })}
          </>
        )}
      </div>
      {showScrollBtn && (
        <button
          onClick={scrollToBottom}
          aria-label="Scroll to bottom"
          className="absolute bottom-4 left-1/2 -translate-x-1/2 border px-[14px] py-[6px] cursor-pointer z-[2]"
          style={{
            background: "var(--surface-raised)",
            borderColor: "var(--border-soft)",
            borderRadius: "var(--radius-full)",
            fontSize: "var(--text-xs)",
            color: "var(--text-secondary)",
            boxShadow: "var(--shadow-md)",
          }}
        >
          {isStreaming && !isFollowing ? "Agent is typing..." : "Scroll to bottom"}
        </button>
      )}
    </div>
  );
};

const EmptyState = () => {
  const appMode = useAppStore((s) => s.appMode);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const currentModel = useAppStore((s) => s.currentModel);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const createConversation = useAppStore((s) => s.createConversation);

  const hasProjectWorkspace = Boolean(workingDirectory.trim());
  const hasModel = Boolean(currentModel.trim());
  const projectName = workspaceDisplayName(workingDirectory, "No workspace");
  const modelLabel = formatModelLabel(currentModel, "Select model");
  const openModelSettings = () => {
    if (!settingsOpen) toggleSettings();
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("minicode:settings-tab", { detail: "provider" }));
    }, 0);
  };
  const startChat = () => createConversation({ appMode, bindWorkspace: hasProjectWorkspace });

  return (
    <div className="workbench-empty-state">
      <section className="workbench-empty-hero">
        <div className="workbench-empty-brand">
          <div className="workbench-empty-mark" aria-hidden="true">
            <Bot size={20} />
          </div>
          <div className="workbench-empty-copy-block">
            <div className="workbench-empty-kicker">MiniCode</div>
            <h1 className="workbench-empty-title">
              What needs attention?
            </h1>
            <p className="workbench-empty-copy">
              {hasProjectWorkspace
                ? `Workspace active: ${projectName}`
                : "Open a workspace or start with a prompt."}
            </p>
          </div>
        </div>

        <div className="workbench-empty-actions">
          <EmptyAction
            icon={<FolderOpen size={15} />}
            title={hasProjectWorkspace ? "Switch workspace" : "Open workspace"}
            detail={hasProjectWorkspace ? projectName : "Choose the folder for this session."}
            onClick={() => void openWorkspaceFolder()}
          />
          <EmptyAction
            icon={<Settings2 size={15} />}
            title={hasModel ? "Model settings" : "Select model"}
            detail={modelLabel}
            onClick={openModelSettings}
          />
          <EmptyAction
            icon={<MessageSquareText size={15} />}
            title="New conversation"
            detail="Start a fresh thread."
            onClick={startChat}
          />
        </div>
      </section>

    </div>
  );
};

const EmptyAction = ({
  icon,
  title,
  detail,
  onClick,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    onClick={onClick}
    className="workbench-empty-action"
  >
    <span className="workbench-empty-action-icon" aria-hidden="true">{icon}</span>
    <span className="workbench-empty-action-body">
      <span className="workbench-empty-action-title">{title}</span>
      <span className="workbench-empty-action-detail">{detail}</span>
    </span>
  </button>
);
