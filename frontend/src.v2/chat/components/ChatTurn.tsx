import { memo, useCallback, useMemo } from "react";
import { Copy, FileDown, RotateCcw, Search } from "lucide-react";
import type {
  AssistantMarkdownCellState,
  ChatTurnState,
  ExecCellState,
  HistoryCellState,
  StreamingAssistantTailCellState,
  UserMessageCellState,
} from "../cells/cellTypes";
import { AgentTurn } from "../../agent-loop/components/AgentTurn";
import type { RenderAgentCellArgs } from "../../agent-loop/components/AgentTurn";
import { projectChatTurnToAgentLoop, isTestCommand } from "../../agent-loop/projection/project-turn";
import { activityGroupMembershipKey, activityGroupStatus } from "../cells/activityGrouping";
import {
  ActivityCell,
  ActivityGroupCell,
  AssistantMarkdownCell,
  DiffCell,
  ErrorCell,
  ExecCell,
  ExecGroupCell,
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
  const committedCells = useMemo(
    () => groupActivityCells(turn.committedCells),
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
  const agentTurn = useMemo(
    () => projectChatTurnToAgentLoop(turn, committedCells),
    [turn, committedCells],
  );
  const renderCell = useCallback(
    ({ key, cell, isActive = false, className }: RenderAgentCellArgs) => (
      <CellWithContextMenu
        key={key}
        cell={cell}
        isActive={isActive}
        className={className}
        onStopExecution={stopActiveRun}
      />
    ),
    [stopActiveRun],
  );

  return (
    <AgentTurn turn={agentTurn} wide={wide} renderCell={renderCell} />
  );
});

function groupActivityCells(cells: ChatTurnState["committedCells"]): ChatTurnState["committedCells"] {
  const grouped: ChatTurnState["committedCells"] = [];
  let buffer: Extract<HistoryCellState, { kind: "activity" }>[] = [];
  let execBuffer: ExecCellState[] = [];

  const flush = () => {
    if (buffer.length === 0) return;

    if (!shouldGroupActivityBuffer(buffer)) {
      buffer.forEach(cell => grouped.push(cell));
    } else {
      const status = activityGroupStatus(buffer);
      grouped.push({
        kind: "activity_group",
        id: `activity-group-${activityGroupMembershipKey(buffer)}-${buffer[0]?.id ?? grouped.length}`,
        cells: buffer,
        status,
        collapsed: status === "done",
        startedAt: Math.min(...buffer.map((cell) => cell.startedAt)),
        completedAt: latestCompletedAt(buffer),
      });
    }
    buffer = [];
  };

  const flushExec = () => {
    if (execBuffer.length === 0) return;

    const status = execGroupStatus(execBuffer);
    grouped.push({
      kind: "exec_group",
      id: `exec-group-${execBuffer[0]?.id ?? grouped.length}`,
      cells: execBuffer,
      status,
      collapsed: status === "done",
      startedAt: Math.min(...execBuffer.map((cell) => cell.createdAt)),
      completedAt: latestExecCompletedAt(execBuffer),
    });
    execBuffer = [];
  };

  for (const cell of cells) {
    if (cell.kind === "activity") {
      flushExec();
      if (!canGroupActivityCell(cell)) {
        flush();
        grouped.push(cell);
        continue;
      }
      const nextKey = activityGroupMembershipKey([cell]);
      const currentKey = buffer.length ? activityGroupMembershipKey(buffer) : nextKey;
      if (buffer.length > 0 && nextKey !== currentKey) flush();
      buffer.push(cell);
      continue;
    }
    if (cell.kind === "exec" && canGroupExecCell(cell)) {
      flush();
      const currentKey = execBuffer.length ? execGroupKey(execBuffer) : execGroupKey([cell]);
      const nextKey = execGroupKey([cell]);
      if (execBuffer.length > 0 && nextKey !== currentKey) flushExec();
      execBuffer.push(cell);
      continue;
    }
    flush();
    flushExec();
    grouped.push(cell);
  }
  flush();
  flushExec();
  return grouped;
}

function canGroupExecCell(cell: ExecCellState): boolean {
  return cell.status !== "pending_approval";
}

function execGroupKey(cells: ExecCellState[]): "test" | "command" {
  return cells.every((cell) => isTestCommand(cell.command)) ? "test" : "command";
}

function execGroupStatus(cells: ExecCellState[]): "running" | "done" | "failed" {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "cancelled")) return "failed";
  if (cells.some((cell) => cell.status === "running" || cell.status === "pending_approval")) return "running";
  return "done";
}

function latestExecCompletedAt(cells: ExecCellState[]): number | undefined {
  const values = cells.map((cell) => cell.completedAt).filter((value): value is number => Number.isFinite(value));
  return values.length ? Math.max(...values) : undefined;
}

function canGroupActivityCell(cell: Extract<HistoryCellState, { kind: "activity" }>): boolean {
  if (cell.status === "failed" || cell.status === "interrupted") return false;
  return activityGroupMembershipKey([cell]) !== "solo";
}

function shouldGroupActivityBuffer(cells: Extract<HistoryCellState, { kind: "activity" }>[]): boolean {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "interrupted")) return false;
  return activityGroupMembershipKey(cells) !== "solo";
}

function latestCompletedAt(cells: Extract<HistoryCellState, { kind: "activity" }>[]): number | undefined {
  const values = cells.map((cell) => cell.completedAt).filter((value): value is number => Number.isFinite(value));
  return values.length ? Math.max(...values) : undefined;
}

// ── CellWithContextMenu ─────────────────────────────────────────────

function CellWithContextMenu({
  cell,
  isActive = false,
  className,
  onStopExecution,
}: {
  cell: HistoryCellState;
  isActive?: boolean;
  className?: string;
  onStopExecution?: () => void;
}) {
  const items = useMemo(() => buildCellMenuItems(cell), [cell]);
  const { onContextMenu, menu } = useContextMenu(items);

  return (
    <div className={className} onContextMenu={onContextMenu} style={{ position: "relative" }}>
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
      return <ActivityGroupCell cells={cell.cells} isActive={isActive} defaultCollapsed={cell.collapsed} />;

    case "plan":
      return <PlanCell cell={cell} />;

    case "exec":
      return <ExecCell cell={cell} isActive={isActive} onStop={onStopExecution} />;

    case "exec_group":
      return <ExecGroupCell cells={cell.cells} isActive={isActive} onStop={onStopExecution} defaultCollapsed={cell.collapsed} />;

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
    <div className="streaming-tail-cell md-prose">
      <MarkdownRenderer content={cell.partialMarkdown} isStreaming />
      <span className="streaming-cursor" />
    </div>
  );
}
