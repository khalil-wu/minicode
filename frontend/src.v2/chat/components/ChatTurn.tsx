import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Copy, FileDown, MessageSquareQuote, RotateCcw, Search, X } from "lucide-react";
import type {
  AssistantMarkdownCellState,
  ChatTurnState,
  DiffCellState,
  HistoryCellState,
  StreamingAssistantTailCellState,
  UserMessageCellState,
} from "../cells/cellTypes";
import { AgentTurn } from "../../agent-loop/components/AgentTurn";
import type { RenderAgentCellArgs } from "../../agent-loop/components/AgentTurn";
import { projectChatTurnToAgentLoop } from "../../agent-loop/projection/project-turn";
import {
  ActivityCell,
  AssistantMarkdownCell,
  DiffCell,
  ErrorCell,
  ExecCell,
  StatusNoticeCell,
  ThinkingCell,
  UserMessageCell,
} from "../cells";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { useContextMenu } from "../../components/useContextMenu";
import type { ContextMenuItem } from "../../components/ContextMenu";
import { useAppStore } from "../../stores";
import type { GitChangesState } from "../../stores/types";
import { sendClientCommand } from "../../protocol/ws-outbox";

// ── ChatTurn ────────────────────────────────────────────────────────

export const ChatTurn = memo(function ChatTurn({
  turn,
  wide = false,
  liveGitChanges,
}: {
  turn: ChatTurnState;
  wide?: boolean;
  liveGitChanges?: GitChangesState["live"];
}) {
  const committedCells = turn.committedCells;
  const committedCellsWithLiveDiff = useMemo(
    () => appendLiveDiffCell(committedCells, liveGitChanges, turn),
    [committedCells, liveGitChanges, turn],
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
    () => projectChatTurnToAgentLoop(turn, committedCellsWithLiveDiff),
    [turn, committedCellsWithLiveDiff],
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

function appendLiveDiffCell(
  cells: ChatTurnState["committedCells"],
  live: GitChangesState["live"] | undefined,
  turn: ChatTurnState,
): ChatTurnState["committedCells"] {
  if (turn.status !== "streaming" || !live?.files?.length) return cells;
  const liveCell = liveDiffCell(live, turn.id);
  if (!liveCell) return cells;
  if (cells.some((cell) => cell.kind === "diff")) return cells;
  return [...cells, liveCell];
}

function liveDiffCell(live: NonNullable<GitChangesState["live"]>, turnId: string): DiffCellState | null {
  const files = mergeLiveDiffFiles(live.files);
  if (files.length === 0) return null;
  return {
    kind: "diff",
    id: `live-diff-${turnId}`,
    status: "updated",
    files,
    summary: {
      added: files.reduce((sum, file) => sum + file.additions, 0),
      deleted: files.reduce((sum, file) => sum + file.deletions, 0),
      modifiedFiles: files.length,
    },
    collapsed: true,
    createdAt: live.updatedAt,
  };
}

function mergeLiveDiffFiles(files: NonNullable<GitChangesState["live"]>["files"]): DiffCellState["files"] {
  const byPath = new Map<string, DiffCellState["files"][number]>();
  for (const file of files) {
    if (!file.path) continue;
    byPath.set(file.path, {
      path: file.path,
      patch: file.patch,
      additions: file.additions,
      deletions: file.deletions,
      isLarge: file.additions + file.deletions > 200,
      isTruncated: file.isBinary,
    });
  }
  return [...byPath.values()];
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
  const cellRef = useRef<HTMLDivElement>(null);
  const { onContextMenu, menu } = useContextMenu(() => {
    const selection = window.getSelection();
    const anchor = selection?.anchorNode;
    const focus = selection?.focusNode;
    const selectedText = anchor && focus
      && cellRef.current?.contains(anchor)
      && cellRef.current?.contains(focus)
      ? selection?.toString().trim() ?? ""
      : "";
    return buildCellMenuItems(cell, selectedText);
  });

  return (
    <div ref={cellRef} className={className} onContextMenu={onContextMenu} style={{ position: "relative" }}>
      <HistoryCellRenderer cell={cell} isActive={isActive} onStopExecution={onStopExecution} />
      {menu}
    </div>
  );
}

function buildCellMenuItems(cell: HistoryCellState, selectedText = ""): ContextMenuItem[] {
  const text = getCellText(cell);
  if (!text) return [];

  // 流式中的assistant_markdown禁用复制（防止复制未闭合代码块）
  const isStreaming = cell.kind === "assistant_markdown" && cell.isStreaming;

  const items: ContextMenuItem[] = [
    {
      label: "Copy message",
      icon: <Copy size={14} />,
      onClick: () => { void navigator.clipboard.writeText(text); },
      disabled: isStreaming,
    },
    {
      label: "Copy as Markdown",
      icon: <FileDown size={14} />,
      onClick: () => {
        const mdSource = cell.kind === "assistant_markdown"
          ? cell.markdownSource
          : text;
        void navigator.clipboard.writeText(mdSource);
      },
      disabled: isStreaming,
    },
  ];

  if (selectedText) {
    items.push({
      label: "Ask about selection",
      icon: <MessageSquareQuote size={14} />,
      onClick: () => {
        useAppStore.getState().openSideChatWithSelection(selectedText, "Conversation selection");
      },
    });
  }

  // Recall is available for user messages and assistant messages with a messageId
  if (cell.kind === "user_message") {
    if (cell.queueState === "queued" && cell.queueMessageId) {
      items.push({
        label: "Cancel queued message",
        icon: <X size={14} />,
        onClick: () => {
          sendClientCommand({
            type: "user_message.queue.cancel",
            conversation_id: useAppStore.getState().conversationId || "",
            message_id: cell.queueMessageId!,
            user_message_id: cell.id,
          });
        },
      });
    } else {
      items.push({
        label: "Recall and edit",
        icon: <RotateCcw size={14} />,
        onClick: () => {
          interruptActiveRunBeforeLocalRecall();
          useAppStore.getState().recallMessage(cell.id);
          queueMicrotask(() => window.dispatchEvent(new Event("composer:focus")));
        },
      });
    }
  } else if (cell.kind === "assistant_markdown" && cell.messageId) {
    items.push({
      label: "Recall and edit",
      icon: <RotateCcw size={14} />,
      onClick: () => {
        interruptActiveRunBeforeLocalRecall();
        useAppStore.getState().recallMessage(cell.messageId!);
        queueMicrotask(() => window.dispatchEvent(new Event("composer:focus")));
      },
    });
  }

  items.push({ separator: true, label: "" });
  items.push({
    label: "Search in conversation",
    icon: <Search size={14} />,
    shortcut: "Ctrl+F",
    onClick: () => {
      window.dispatchEvent(new CustomEvent("chat:request-search"));
    },
  });

  return items;
}

function interruptActiveRunBeforeLocalRecall(): void {
  const state = useAppStore.getState();
  if (!state.isStreaming) return;
  state.interrupt();
  sendClientCommand({
    type: "interrupt",
    conversation_id: state.conversationId || undefined,
  });
}

function getCellText(cell: HistoryCellState): string | null {
  switch (cell.kind) {
    case "user_message":
      return cell.content;
    case "assistant_markdown":
      return cell.markdownSource;
    case "streaming_assistant_tail":
      return cell.partialMarkdown;
    case "streaming_assistant_narration":
      return cell.partialMarkdown;
    case "thinking":
      return cell.content;
    case "error":
      return cell.message || cell.title;
    case "exec":
      return cell.command;
    case "status_notice":
      return cell.message ? `${cell.title}: ${cell.message}` : cell.title;
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

    case "thinking":
      return <ThinkingCell cell={cell} isStreaming={cell.isStreaming || isActive} />;

    case "activity":
      return <ActivityCell cell={cell} isActive={isActive} />;

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

    case "streaming_assistant_narration":
      return (
        <div className="agent-loop-process-note agent-loop-live-narration" data-status="running">
          <MarkdownRenderer content={cell.partialMarkdown} isStreaming />
        </div>
      );

    default:
      return null;
  }
}

// ── Streaming Tail (Phase 1 thin component) ─────────────────────────

/**
 * Truncate streaming text to the last complete line (ends with \n).
 * This mirrors cc's visibleStreamingText approach: hide the incomplete
 * last line to avoid re-parsing/re-rendering it on every token, which
 * causes visual jitter — especially when the incomplete line contains
 * markdown syntax (e.g. an unclosed ``` code fence).
 *
 * If there's no newline at all, show the full text (short single-line
 * replies shouldn't be hidden).
 *
 * When the user prefers reduced motion, skip truncation entirely —
 * they want to see content as it arrives without the "typing" effect.
 */
function truncateToLastCompleteLine(text: string, reducedMotion: boolean): string {
  if (reducedMotion || text.length <= 80) return text;
  const lastNewline = text.lastIndexOf("\n");
  if (lastNewline < 0) return text;
  // Keep everything up to and including the last newline.
  return text.substring(0, lastNewline + 1);
}

/**
 * Detects the user's prefers-reduced-motion setting. When true, the
 * streaming cursor is hidden and text is not truncated to the last
 * complete line. Mirrors cc's reducedMotion check (REPL.tsx:1494).
 */
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return reduced;
}

const StreamingAssistantTailCell = memo(function StreamingAssistantTailCell({
  cell,
}: {
  cell: StreamingAssistantTailCellState;
}) {
  const reducedMotion = usePrefersReducedMotion();
  const visibleContent = truncateToLastCompleteLine(cell.partialMarkdown, reducedMotion);
  return (
    <div className="streaming-tail-cell md-prose">
      <MarkdownRenderer content={visibleContent} isStreaming />
      {!reducedMotion && <span className="streaming-cursor" />}
    </div>
  );
});
