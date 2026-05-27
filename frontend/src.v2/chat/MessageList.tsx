import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Bot, Command, FileText, GitBranch, TerminalSquare } from "lucide-react";
import { useAppStore } from "../stores";
import { UserMessage } from "./messages/UserMessage";
import { AssistantMessage } from "./messages/AssistantMessage";
import { SystemNotice } from "./messages/SystemNotice";
import { branchDisplayName, workspaceDisplayName } from "../lib/workspace-display";

export const MessageList = () => {
  const messages = useAppStore((s) => s.messages);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const conversationId = useAppStore((s) => s.conversationId);
  const ref = useRef<HTMLDivElement>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  const [isFollowing, setIsFollowing] = useState(true);
  const isNearBottom = useRef(true);
  const prevConvId = useRef(conversationId);
  const [faded, setFaded] = useState(false);

  useLayoutEffect(() => {
    if (prevConvId.current !== conversationId) {
      prevConvId.current = conversationId;
      setFaded(true);
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
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      isNearBottom.current = gap < 80;
      setIsFollowing(gap < 80);
      setShowScrollBtn(gap > 200);
    };
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) {
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
      style={{
        position: "relative",
        flex: 1,
        minHeight: 0,
        height: "100%",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        ref={ref}
        data-testid="message-list-scroll"
        className="message-list-scroll"
        role="log"
        aria-label="Conversation history"
        tabIndex={0}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          overscrollBehavior: "contain",
          scrollbarGutter: "stable",
          padding: "58px 28px 34px",
          display: "flex",
          flexDirection: "column",
          gap: 30,
          background: "var(--surface-base)",
          opacity: faded ? 0 : 1,
          transition: "opacity 120ms ease-out",
        }}
      >
        {messages.length === 0 ? (
          <>
            <EmptyState />
          </>
        ) : (
          <>
            {messages.map((m, i) => (
              <div
                key={m.id}
                className={i >= messages.length - 3 ? "message-enter" : undefined}
                style={{
                  width: m.role === "system" ? "fit-content" : "min(980px, 100%)",
                  maxWidth: "min(980px, 100%)",
                  margin: m.role === "system" ? "0" : "0 auto",
                  contentVisibility: i < messages.length - 10 ? "auto" : "visible",
                  containIntrinsicSize: "auto 120px",
                }}
              >
                {m.role === "user" ? (
                  <UserMessage message={m} />
                ) : m.role === "system" ? (
                  <SystemNotice message={m} />
                ) : (
                  <AssistantMessage message={m} />
                )}
              </div>
            ))}
          </>
        )}
      </div>
      {showScrollBtn && (
        <button
          onClick={scrollToBottom}
          aria-label="Scroll to bottom"
          style={{
            position: "absolute",
            bottom: 16,
            left: "50%",
            transform: "translateX(-50%)",
            background: "var(--surface-raised)",
            border: "1px solid var(--border-soft)",
            borderRadius: "var(--radius-full)",
            padding: "6px 14px",
            cursor: "pointer",
            fontSize: "var(--text-xs)",
            color: "var(--text-secondary)",
            boxShadow: "var(--shadow-md)",
            zIndex: 2,
          }}
        >
          {isStreaming && !isFollowing ? "↓ Agent is typing..." : "Scroll to bottom"}
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

  const projectName = workspaceDisplayName(workingDirectory, "Current workspace");
  const shortModel = currentModel
    ? currentModel.replace(/^(claude-|gpt-|gemini-)/, "").split("-").slice(0, 2).join("-")
    : "No model";

  return (
    <div style={emptyShellStyle}>
      <div style={sessionHeaderStyle}>
        <div style={agentMarkStyle}>
          <Bot size={18} />
        </div>
        <div style={{ minWidth: 0 }}>
          <div style={{ color: "var(--text-primary)", fontWeight: 650, fontSize: "var(--text-md)" }}>
            Ready in {projectName}
          </div>
          <div style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)", marginTop: 3 }}>
            Ask for a change, inspect files, or switch modes from the footer.
          </div>
        </div>
      </div>

      <div style={metadataGridStyle}>
        <MetaItem label="model" value={shortModel} />
        <MetaItem label="mode" value={permissionModeLabel(permissionMode)} />
        <MetaItem label="branch" value={branchDisplayName(workspaceGit?.branch) || "--"} icon={<GitBranch size={13} />} />
        <MetaItem label="mcp" value={mcpServers.length ? `${mcpServers.length}` : "--"} />
        <MetaItem label="terminal" value={terminalSessions.length ? `${terminalSessions.length}` : "--"} icon={<TerminalSquare size={13} />} />
      </div>

      <div style={promptGridStyle}>
        <PromptHint icon={<Command size={15} />} title="Plan mode" detail="Switch from the footer or Settings" />
        <PromptHint icon={<FileText size={15} />} title="@file" detail="Reference workspace context" />
        <PromptHint icon={<TerminalSquare size={15} />} title="Ctrl+J" detail="Open the terminal stack" />
      </div>
    </div>
  );
};

const MetaItem = ({ label, value, icon }: { label: string; value: string; icon?: React.ReactNode }) => (
  <div style={metaItemStyle}>
    <span style={{ color: "var(--text-muted)", display: "inline-flex", alignItems: "center", gap: 5 }}>
      {icon}
      {label}
    </span>
    <span style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
      {value}
    </span>
  </div>
);

const PromptHint = ({ icon, title, detail }: { icon: React.ReactNode; title: string; detail: string }) => (
  <div style={promptHintStyle}>
    <span style={{ color: "var(--accent-primary)", display: "inline-flex" }}>{icon}</span>
    <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{title}</span>
    <span style={{ color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{detail}</span>
  </div>
);

const permissionModeLabel = (mode: string): string => {
  if (mode === "ask_permissions") return "Ask";
  if (mode === "acceptEdits") return "Accept";
  if (mode === "plan") return "Plan";
  if (mode === "bypass") return "Bypass";
  return "Auto";
};

const emptyShellStyle: React.CSSProperties = {
  width: "min(980px, 100%)",
  margin: "auto",
  display: "grid",
  gap: 16,
};

const sessionHeaderStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  paddingBottom: 12,
  borderBottom: "1px solid var(--border-subtle)",
};

const agentMarkStyle: React.CSSProperties = {
  width: 34,
  height: 34,
  borderRadius: "var(--radius-sm, 6px)",
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
  color: "var(--accent-primary)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
};

const metadataGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(5, minmax(0, 1fr))",
  gap: 6,
};

const metaItemStyle: React.CSSProperties = {
  display: "grid",
  gap: 3,
  minWidth: 0,
  padding: "8px 9px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-page)",
  fontSize: "var(--text-xs)",
};

const promptGridStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
  gap: 8,
};

const promptHintStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "auto auto 1fr",
  alignItems: "center",
  gap: 7,
  minWidth: 0,
  padding: "9px 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-page)",
  fontSize: "var(--text-xs)",
};
