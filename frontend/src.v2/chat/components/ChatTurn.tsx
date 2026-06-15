import { memo, useCallback, useMemo } from "react";
import type React from "react";
import { Copy, FileDown, RotateCcw, Search } from "lucide-react";
import type {
  AssistantMarkdownCellState,
  ChatTurnState,
  HistoryCellState,
  StreamingAssistantTailCellState,
  UserMessageCellState,
} from "../cells/cellTypes";
import {
  ActivityCell,
  ActivityGroupCell,
  AssistantMarkdownCell,
  DiffCell,
  ErrorCell,
  ExecCell,
  PlanCell,
  StatusNoticeCell,
  ThinkingCell,
  TurnSummaryCell,
  UserMessageCell,
} from "../cells";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { useContextMenu } from "../../components/useContextMenu";
import type { ContextMenuItem } from "../../components/ContextMenu";
import { useAppStore } from "../../stores";
import { sendClientCommand } from "../../protocol/ws-outbox";

// ── ChatTurn ────────────────────────────────────────────────────────

export const ChatTurn = memo(function ChatTurn({
  turn,
  wide = false,
}: {
  turn: ChatTurnState;
  wide?: boolean;
}) {
  const committedCellEntries = useMemo(
    () => withStableRenderKeys(groupActivityCells(turn.committedCells)),
    [turn.committedCells],
  );
  const stopActiveRun = useCallback(() => {
    const state = useAppStore.getState();
    const conversationId = state.conversationId || undefined;
    state.interrupt();
    sendClientCommand({
      type: "interrupt",
      conversation_id: conversationId,
    });
  }, []);

  return (
    <div className="chat-turn" style={turnStyle(wide)}>
      {turn.userCell && <CellWithContextMenu cell={turn.userCell} />}
      {committedCellEntries.map(({ cell, key }) => (
        <CellWithContextMenu key={key} cell={cell} onStopExecution={stopActiveRun} />
      ))}
      {turn.activeCell && (
        <CellWithContextMenu cell={turn.activeCell} isActive onStopExecution={stopActiveRun} />
      )}
      {turn.finalAnswerCell && (
        <CellWithContextMenu cell={turn.finalAnswerCell} />
      )}
      {/* Streaming text that hasn't formed a final answer yet */}
      {turn.status === "streaming" &&
        !turn.activeCell &&
        !turn.finalAnswerCell && <StreamingIndicator />}
    </div>
  );
});

function groupActivityCells(cells: ChatTurnState["committedCells"]): ChatTurnState["committedCells"] {
  const grouped: ChatTurnState["committedCells"] = [];
  let buffer: Extract<HistoryCellState, { kind: "activity" }>[] = [];

  const flush = () => {
    if (buffer.length === 0) return;

    // ✅ P0优化：减少过度聚合
    // 规则1：正在执行的工具不聚合（用户需要看到实时进度）
    if (buffer.some(cell => cell.status === "running")) {
      buffer.forEach(cell => grouped.push(cell));
      buffer = [];
      return;
    }

    // 规则2：失败的工具不聚合（需要突出显示）
    if (buffer.some(cell => cell.status === "failed" || cell.status === "interrupted")) {
      buffer.forEach(cell => grouped.push(cell));
      buffer = [];
      return;
    }

    // 规则3：只有3个以上的同类已完成工具才聚合
    if (buffer.length === 1 || buffer.length === 2) {
      buffer.forEach(cell => grouped.push(cell));
    } else {
      // 检查是否同类工具
      const firstKind = buffer[0]?.activityKind;
      const allSameKind = buffer.every(cell => cell.activityKind === firstKind);

      if (allSameKind) {
        // 同类工具聚合
        grouped.push({
          kind: "activity_group",
          id: `activity-group-${buffer[0]?.id ?? grouped.length}`,
          cells: buffer,
          status: groupStatus(buffer),
          collapsed: true,
          startedAt: Math.min(...buffer.map((cell) => cell.startedAt)),
          completedAt: latestCompletedAt(buffer),
        });
      } else {
        // 不同类工具不聚合
        buffer.forEach(cell => grouped.push(cell));
      }
    }
    buffer = [];
  };

  for (const cell of cells) {
    if (cell.kind === "activity") {
      buffer.push(cell);
      continue;
    }
    flush();
    grouped.push(cell);
  }
  flush();
  return grouped;
}

function withStableRenderKeys(cells: ChatTurnState["committedCells"]) {
  const seen = new Map<string, number>();
  return cells.map((cell) => {
    const base = `${cell.kind}:${cell.id}`;
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    return {
      cell,
      key: count === 0 ? base : `${base}:${count}`,
    };
  });
}

function groupStatus(cells: Extract<HistoryCellState, { kind: "activity" }>[]) {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "interrupted")) return "failed";
  if (cells.some((cell) => cell.status === "running")) return "running";
  return "done";
}

function latestCompletedAt(cells: Extract<HistoryCellState, { kind: "activity" }>[]): number | undefined {
  const values = cells.map((cell) => cell.completedAt).filter((value): value is number => Number.isFinite(value));
  return values.length ? Math.max(...values) : undefined;
}

// ── CellWithContextMenu ─────────────────────────────────────────────

function CellWithContextMenu({
  cell,
  isActive = false,
  onStopExecution,
}: {
  cell: HistoryCellState;
  isActive?: boolean;
  onStopExecution?: () => void;
}) {
  const items = useMemo(() => buildCellMenuItems(cell), [cell]);
  const { onContextMenu, menu } = useContextMenu(items);

  return (
    <div onContextMenu={onContextMenu} style={{ position: "relative" }}>
      <HistoryCellRenderer cell={cell} isActive={isActive} onStopExecution={onStopExecution} />
      {menu}
    </div>
  );
}

function buildCellMenuItems(cell: HistoryCellState): ContextMenuItem[] {
  const text = getCellText(cell);
  if (!text) return [];

  // 流式中的assistant_markdown禁用复制（防止复制未闭合代码块）
  const isStreaming = cell.kind === "assistant_markdown" && cell.isStreaming;

  const items: ContextMenuItem[] = [
    {
      label: "Copy message",
      icon: <Copy size={13} />,
      onClick: () => { void navigator.clipboard.writeText(text); },
      disabled: isStreaming,
    },
    {
      label: "Copy as Markdown",
      icon: <FileDown size={13} />,
      onClick: () => {
        const mdSource = cell.kind === "assistant_markdown"
          ? cell.markdownSource
          : text;
        void navigator.clipboard.writeText(mdSource);
      },
      disabled: isStreaming,
    },
  ];

  // Recall is available for user messages and assistant messages with a messageId
  if (cell.kind === "user_message") {
    items.push({
      label: "Recall and edit",
      icon: <RotateCcw size={13} />,
      onClick: () => { useAppStore.getState().recallMessage(cell.id); },
    });
  } else if (cell.kind === "assistant_markdown" && cell.messageId) {
    items.push({
      label: "Recall and edit",
      icon: <RotateCcw size={13} />,
      onClick: () => { useAppStore.getState().recallMessage(cell.messageId!); },
    });
  }

  items.push({ separator: true, label: "" });
  items.push({
    label: "Search in conversation",
    icon: <Search size={13} />,
    shortcut: "Ctrl+F",
    onClick: () => {
      window.dispatchEvent(new CustomEvent("chat:request-search"));
    },
  });

  return items;
}

function getCellText(cell: HistoryCellState): string | null {
  switch (cell.kind) {
    case "user_message":
      return cell.content;
    case "assistant_markdown":
      return cell.markdownSource;
    case "streaming_assistant_tail":
      return cell.partialMarkdown;
    case "thinking":
      return cell.content;
    case "error":
      return cell.message || cell.title;
    case "exec":
      return cell.command;
    case "status_notice":
      return cell.message ? `${cell.title}: ${cell.message}` : cell.title;
    case "turn_summary":
      return cell.items.map((item) => [item.label, item.detail].filter(Boolean).join(" ")).join(", ");
    default:
      return null;
  }
}

// ── HistoryCellRenderer ─────────────────────────────────────────────

export function HistoryCellRenderer({
  cell,
  isActive = false,
  onStopExecution,
}: {
  cell: HistoryCellState;
  isActive?: boolean;
  onStopExecution?: () => void;
}) {
  switch (cell.kind) {
    case "user_message":
      return <UserMessageCell cell={cell} />;

    case "status_notice":
      return <StatusNoticeCell cell={cell} />;

    case "turn_summary":
      return <TurnSummaryCell cell={cell} />;

    case "thinking":
      return <ThinkingCell cell={cell} isStreaming={isActive} />;

    case "activity":
      return <ActivityCell cell={cell} isActive={isActive} />;

    case "activity_group":
      return <ActivityGroupCell cells={cell.cells} isActive={isActive} />;

    case "plan":
      return <PlanCell cell={cell} />;

    case "exec":
      return <ExecCell cell={cell} isActive={isActive} onStop={onStopExecution} />;

    case "diff":
      return <DiffCell cell={cell} />;

    case "error":
      return <ErrorCell cell={cell} />;

    case "assistant_markdown":
      return <AssistantMarkdownCell cell={cell} />;

    case "streaming_assistant_tail":
      return <StreamingAssistantTailCell cell={cell} />;

    default:
      return null;
  }
}

// ── Streaming Tail (Phase 1 thin component) ─────────────────────────

function StreamingAssistantTailCell({
  cell,
}: {
  cell: StreamingAssistantTailCellState;
}) {
  return (
    <div className="md-prose" style={streamingTailStyle}>
      <MarkdownRenderer content={cell.partialMarkdown} isStreaming />
      <span className="streaming-cursor" />
    </div>
  );
}

function StreamingIndicator() {
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "2px 8px",
        color: "var(--text-muted)",
        fontStyle: "italic",
        fontSize: "var(--text-sm, 13px)",
        opacity: 0.6,
      }}
    >
      <span className="thinking-mini-dot" />
      <span>Thinking...</span>
    </div>
  );
}

// ── Styles ──────────────────────────────────────────────────────────

const turnStyle = (wide: boolean): React.CSSProperties => ({
  display: "flex",
  flexDirection: "column",
  gap: 10,
  marginBottom: 22,
  width: wide ? "min(1320px, 100%)" : "min(880px, 100%)",
  margin: "0 auto",
});

const streamingTailStyle: React.CSSProperties = {
  color: "var(--text-primary)",
  fontSize: "var(--text-md, 14px)",
  lineHeight: 1.75,
  wordBreak: "break-word",
};
