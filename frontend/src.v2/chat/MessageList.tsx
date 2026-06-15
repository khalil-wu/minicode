import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Bot, FileText, FolderOpen, GitBranch, MessageSquareText, Settings2, ShieldCheck, TerminalSquare } from "lucide-react";
import { useAppStore } from "../stores";
import { formatModelLabel } from "../lib/model-label";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";
import { projectMessagesToTurns, projectRecentMessagesToTurns } from "./chatSurfaceState";
import { ChatTurn } from "./components/ChatTurn";
import { InlineTaskList } from "./components/InlineTaskList";
import { openWorkspaceFolder } from "../workspace/openWorkspaceFolder";

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

  // 如果没有active conversation且conversations为空，不显示messages
  const shouldShowMessages = conversationId !== null || conversations.length > 0;

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
          paddingBottom: appMode === "code" ? 220 : 180,
          overscrollBehavior: "contain",
          scrollbarGutter: "stable",
          background: "var(--surface-base)",
          opacity: faded ? 0 : 1,
        }}
      >
        {messages.length === 0 ? (
          <>
            <EmptyState />
          </>
        ) : (
          <>
            <InlineTaskList />
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
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const currentModel = useAppStore((s) => s.currentModel);
  const permissionMode = useAppStore((s) => s.permissionMode);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const terminalSessions = useAppStore((s) => s.terminalSessions);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const appMode = useAppStore((s) => s.appMode);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const createConversation = useAppStore((s) => s.createConversation);
  const setDraft = useAppStore((s) => s.setDraft);

  const hasProjectWorkspace = Boolean(workingDirectory.trim());
  const hasModel = Boolean(currentModel.trim());
  const projectName = workspaceDisplayName(workingDirectory, "Computer");
  const modelLabel = formatModelLabel(currentModel, "Select model");
  const wide = appMode === "cowork" || appMode === "code";
  const starterPrompt = hasProjectWorkspace
    ? "Review this workspace and suggest the safest next release-hardening step."
    : "Help me turn this task into a concrete plan, then ask for the first file or folder if needed.";
  const openModelSettings = () => {
    if (!settingsOpen) toggleSettings();
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("minicode:settings-tab", { detail: "provider" }));
    }, 0);
  };
  const startChat = () => {
    if (!useAppStore.getState().conversationId) {
      createConversation({ bindWorkspace: hasProjectWorkspace });
    }
    setDraft(starterPrompt);
  };

  return (
    <div className="mx-auto my-auto grid gap-4" style={{ width: wide ? "min(1320px, 100%)" : "min(980px, 100%)" }}>
      <div className="flex items-center gap-3 pb-3" style={{ borderBottom: "1px solid var(--border-subtle)" }}>
        <div
          className="w-[34px] h-[34px] inline-flex items-center justify-center flex-shrink-0 border"
          style={{
            borderRadius: "var(--radius-sm, 6px)",
            borderColor: "var(--border-subtle)",
            background: "var(--surface-page)",
            color: "var(--accent-primary)",
          }}
        >
          <Bot size={18} />
        </div>
        <div className="min-w-0">
          <div className="font-[650]" style={{ color: "var(--text-primary)", fontSize: "var(--text-md)" }}>
            {hasProjectWorkspace ? `Ready in ${projectName}` : "Ready to build"}
          </div>
          <div className="mt-[3px]" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
            {hasProjectWorkspace
              ? "Ask for a change, inspect files, or run a focused release check."
              : "Start with a workspace, a model, or a plain-English task."}
          </div>
        </div>
      </div>

      <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))" }}>
        <ActionCard
          icon={<FolderOpen size={16} />}
          title={hasProjectWorkspace ? "Workspace open" : "Open workspace"}
          detail={hasProjectWorkspace ? projectName : "Choose the code folder MiniCode should inspect and edit."}
          actionLabel={hasProjectWorkspace ? "Switch folder" : "Open folder"}
          onClick={() => void openWorkspaceFolder()}
        />
        <ActionCard
          icon={<Settings2 size={16} />}
          title={hasModel ? "Model ready" : "Select model"}
          detail={hasModel ? modelLabel : "Add a provider key and choose the model before sending."}
          actionLabel="Models"
          onClick={openModelSettings}
          tone={hasModel ? "normal" : "warning"}
        />
        <ActionCard
          icon={<MessageSquareText size={16} />}
          title="Start chat"
          detail={hasProjectWorkspace ? "Seed the composer with a release-review prompt." : "Seed the composer with a planning prompt."}
          actionLabel="Use starter"
          onClick={startChat}
        />
      </div>

      <div className="grid gap-[6px]" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(132px, 1fr))" }}>
        <MetaItem label="model" value={modelLabel} />
        <MetaItem label="mode" value={permissionModeLabel(permissionMode)} />
        {hasProjectWorkspace && <MetaItem label="branch" value={branchDisplayName(workspaceGit?.branch) || "--"} icon={<GitBranch size={13} />} />}
        {mcpServers.length > 0 && <MetaItem label="mcp" value={`${mcpServers.length}`} />}
        {terminalSessions.length > 0 && <MetaItem label="terminal" value={`${terminalSessions.length}`} icon={<TerminalSquare size={13} />} />}
      </div>

      <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))" }}>
        <PromptHint icon={<ShieldCheck size={15} />} title="Permissions" detail="Ask, Auto, or Full access" />
        <PromptHint icon={<FileText size={15} />} title="@file" detail={hasProjectWorkspace ? "Reference workspace context" : "Attach or mention a path"} />
        <PromptHint icon={<TerminalSquare size={15} />} title="Ctrl+J" detail="Open the terminal stack" />
      </div>
    </div>
  );
};

const ActionCard = ({
  icon,
  title,
  detail,
  actionLabel,
  onClick,
  tone = "normal",
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
  actionLabel: string;
  onClick: () => void;
  tone?: "normal" | "warning";
}) => (
  <button
    type="button"
    onClick={onClick}
    className="grid gap-2 text-left border cursor-pointer"
    style={{
      minHeight: 118,
      padding: "13px 14px",
      borderColor: tone === "warning" ? "color-mix(in oklch, var(--state-warning) 45%, var(--border-subtle))" : "var(--border-subtle)",
      borderRadius: "var(--radius-md, 10px)",
      background: tone === "warning"
        ? "color-mix(in oklch, var(--state-warning) 9%, var(--surface-page))"
        : "var(--surface-page)",
      color: "var(--text-primary)",
      fontFamily: "var(--font-ui)",
    }}
  >
    <span className="inline-flex items-center gap-2" style={{ color: tone === "warning" ? "var(--state-warning)" : "var(--accent-primary)" }}>
      {icon}
      <span style={{ fontWeight: 700, fontSize: "var(--text-sm)" }}>{title}</span>
    </span>
    <span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)", lineHeight: 1.45 }}>{detail}</span>
    <span style={{ color: tone === "warning" ? "var(--state-warning)" : "var(--accent-primary)", fontSize: "var(--text-xs)", fontWeight: 700 }}>
      {actionLabel}
    </span>
  </button>
);

const MetaItem = ({ label, value, icon }: { label: string; value: string; icon?: React.ReactNode }) => (
  <div
    className="grid gap-[5px] min-w-0 px-[11px] py-[9px] border"
    style={{
      borderColor: "var(--border-subtle)",
      borderRadius: "var(--radius-sm, 6px)",
      background: "var(--surface-page)",
    }}
  >
    <span className="inline-flex items-center gap-[5px]" style={{ color: "var(--text-muted)", fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 600 }}>
      {icon}
      {label}
    </span>
    <span className="overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)" }}>
      {value}
    </span>
  </div>
);

const PromptHint = ({ icon, title, detail }: { icon: React.ReactNode; title: string; detail: string }) => (
  <div
    className="grid grid-cols-[auto_auto_1fr] items-center gap-[7px] min-w-0 px-[10px] py-[9px] border"
    style={{
      borderColor: "var(--border-subtle)",
      borderRadius: "var(--radius-sm, 4px)",
      background: "var(--surface-page)",
      fontSize: "var(--text-xs)",
    }}
  >
    <span className="inline-flex" style={{ color: "var(--accent-primary)" }}>{icon}</span>
    <span className="font-semibold" style={{ color: "var(--text-primary)" }}>{title}</span>
    <span className="overflow-hidden text-ellipsis whitespace-nowrap" style={{ color: "var(--text-muted)" }}>{detail}</span>
  </div>
);

const permissionModeLabel = (mode: string): string => {
  if (mode === "ask_permissions") return "Ask";
  if (mode === "bypass") return "Full access";
  return "Auto";
};
