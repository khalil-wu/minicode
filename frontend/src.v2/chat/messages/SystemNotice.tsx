import { AlertTriangle, Info } from "lucide-react";
import { memo } from "react";
import type { ChatMessage } from "../../stores/types";
import { MarkdownRenderer } from "./MarkdownRenderer";

type NoticeKind = "error" | "compact" | "info";

function detectKind(content: string): NoticeKind {
  if (content.startsWith("Error:") || content.includes("LLM API")) return "error";
  if (content.startsWith("Context compacted") || content.startsWith("Compacting context")) return "compact";
  return "info";
}

const STYLE: Record<NoticeKind, { icon: "error" | "info"; border: string; bg: string; color: string }> = {
  error: {
    icon: "error",
    border: "var(--state-danger)",
    bg: "color-mix(in oklch, var(--state-danger) 8%, var(--surface-soft))",
    color: "var(--state-danger)",
  },
  compact: {
    icon: "info",
    border: "var(--state-info)",
    bg: "color-mix(in oklch, var(--state-info) 8%, var(--surface-soft))",
    color: "var(--state-info)",
  },
  info: {
    icon: "info",
    border: "var(--border-subtle)",
    bg: "var(--surface-soft)",
    color: "var(--text-muted)",
  },
};

export const SystemNotice = memo(({ message }: { message: ChatMessage }) => {
  const kind = detectKind(message.content);
  const s = STYLE[kind];
  const hasCodeBlock = message.content.includes("```");
  const Icon = s.icon === "error" ? AlertTriangle : Info;

  return (
    <div style={noticeStyle(s)}>
      <Icon size={13} style={{ flexShrink: 0, marginTop: 2 }} />
      <div style={{ flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word", minWidth: 0 }}>
        {hasCodeBlock ? <MarkdownRenderer content={message.content} /> : message.content}
      </div>
    </div>
  );
});

SystemNotice.displayName = "SystemNotice";

const noticeStyle = (s: { border: string; bg: string; color: string }): React.CSSProperties => ({
  display: "inline-flex",
  alignItems: "flex-start",
  gap: 7,
  maxWidth: "100%",
  padding: "6px 9px",
  background: s.bg,
  border: `1px solid ${s.border}`,
  borderRadius: "var(--radius-sm, 6px)",
  fontSize: "var(--text-xs)",
  color: s.color,
});
