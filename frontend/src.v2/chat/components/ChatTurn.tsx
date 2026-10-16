import { memo, useCallback, useMemo, useRef } from "react";
import { loadEarlierToolItems } from "../historyPagination";
import type {
  ChatTurnState,
  ActivityCellState,
  HistoryCellState,
  UserMessageCellState,
} from "../cells/cellTypes";
import { AgentTurn } from "../../agent-loop/components/AgentTurn";
import type { RenderAgentCellArgs } from "../../agent-loop/components/AgentTurn";
import { projectChatTurnToAgentLoop } from "../../agent-loop/projection/project-turn";
import {
  ActivityCell,
  AssistantMarkdownCell,
  CollaborationCell,
  DiffCell,
  ErrorCell,
  ExecCell,
  StatusNoticeCell,
  ContextCompactionCell,
  GuardianReviewCell,
  GuardianWarningCell,
  ThinkingCell,
  UserMessageCell,
} from "../cells";
import { useAppStore } from "../../stores";
import { knownFilePathsForCell } from "../cells/activityCellHelpers";
import { conversationReadingState, createConversationReadingState, TranscriptConversationReadingContext, TranscriptReadingContext, turnReadingState } from "../transcriptReadingState";
import { McpAppInlineSlot } from "../mcp-apps/app-surfaces";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";

// ── ChatTurn ────────────────────────────────────────────────────────

export const ChatTurn = memo(function ChatTurn({
  turn,
  wide = false,
  defaultProcessExpanded,
  isTranscriptMode = false,
  allowFileRevert = !isTranscriptMode,
  conversationId,
  workspaceRoot,
}: {
  turn: ChatTurnState;
  wide?: boolean;
  defaultProcessExpanded?: boolean;
  isTranscriptMode?: boolean;
  allowFileRevert?: boolean;
  /** Conversation that owns artifacts in this transcript.  This is explicit
   * because transcript cells can be rendered while another conversation is
   * active (for example, a child-agent replay). */
  conversationId?: string;
  /** Workspace that owns file paths projected into this transcript. */
  workspaceRoot?: string;
}) {
  const committedCells = turn.committedCells;
  const conversationReading = useMemo(() => conversationId ? conversationReadingState(conversationId) : createConversationReadingState(), [conversationId]);
  const readingState = useMemo(() => conversationId ? turnReadingState(conversationId, turn.id) : new Map<string, unknown>(), [conversationId, turn.id]);
  const loadingTools = useRef(false);
  const hasMoreHistory = Boolean(turn.toolPage?.remaining && conversationId);
  const loadHistory = useCallback(() => {
    if (!conversationId || !turn.toolPage?.remaining || loadingTools.current) return;
    loadingTools.current = true;
    void loadEarlierToolItems(conversationId, turn.id).finally(() => { loadingTools.current = false; });
  }, [conversationId, turn.id, turn.toolPage]);
  const processDetailMode = useAppStore((state) => state.viewMode);
  const agentTurn = useMemo(() => {
    const projected = projectChatTurnToAgentLoop(turn, committedCells, processDetailMode);
    // The child provider receives its parent's dispatch as user input, but the
    // child work record must never present that dispatch as the human's bubble.
    return isTranscriptMode ? { ...projected, userCell: null,
      workItems: projected.workItems?.filter((item) => item.kind === "process"),
    } : projected;
  }, [turn, committedCells, processDetailMode, isTranscriptMode]);
  const resourceKey = turn.resourceKey ?? agentTurn.processCells;
  const mcpApps = useMemo(() => [...new Map(agentTurn.processCells.flatMap(cell => cell.kind === "activity"
    ? (cell.toolCallRecords ?? []).flatMap(record => record.mcpApp ? [[record.mcpApp.id, { cell, record }] as const] : [])
    : [])).values()], [agentTurn.processCells]);
  const knownFilePaths = useMemo(() => [...new Set(agentTurn.processCells.flatMap(knownFilePathsForCell))], [resourceKey]);
  const renderCell = useCallback(
    ({ key, cell, isActive = false, className, afterContent }: RenderAgentCellArgs) => (
      <div key={key} className={className} style={{ position: "relative" }}>
        <HistoryCellRenderer
          cell={cell}
          isActive={isActive}
          isTranscriptMode={isTranscriptMode}
          allowFileRevert={allowFileRevert}
          conversationId={conversationId}
          workspaceRoot={workspaceRoot}
          knownFilePaths={knownFilePaths}
          afterContent={afterContent}
        />
      </div>
    ),
    [conversationId, isTranscriptMode, allowFileRevert, workspaceRoot, knownFilePaths],
  );

  return (
    <TranscriptConversationReadingContext.Provider value={conversationReading}>
    <TranscriptReadingContext.Provider key={`${conversationId ?? ""}:${turn.id}`} value={readingState}>
    <AgentTurn
      turn={agentTurn}
      wide={wide}
      renderCell={renderCell}
      defaultProcessExpanded={defaultProcessExpanded}
      hasMoreHistory={hasMoreHistory}
      onLoadHistory={hasMoreHistory ? loadHistory : undefined}
      persistentContent={conversationId && mcpApps.length > 0 ? <div className="agent-loop-persistent-apps">
        {mcpApps.map(({ cell, record }) => <McpAppActivity key={record.mcpApp!.id} cell={cell} record={record}
          conversationId={conversationId} workspaceRoot={workspaceRoot} readOnly={isTranscriptMode} />)}
      </div> : undefined}
    />
    </TranscriptReadingContext.Provider>
    </TranscriptConversationReadingContext.Provider>
  );
});

// ── HistoryCellRenderer ─────────────────────────────────────────────

export const HistoryCellRenderer = memo(function HistoryCellRenderer({
  cell,
  isActive = false,
  isTranscriptMode = false,
  allowFileRevert = !isTranscriptMode,
  conversationId,
  workspaceRoot,
  knownFilePaths,
  afterContent,
}: {
  cell: HistoryCellState;
  isActive?: boolean;
  isTranscriptMode?: boolean;
  allowFileRevert?: boolean;
  conversationId?: string;
  workspaceRoot?: string;
  knownFilePaths?: string[];
  afterContent?: React.ReactNode;
}) {
  switch (cell.kind) {
    case "user_message":
      return <UserMessageCell cell={cell} isTranscriptMode={isTranscriptMode} conversationId={conversationId} />;

    case "status_notice":
      return <StatusNoticeCell cell={cell} />;

    case "guardian_review": return <GuardianReviewCell cell={cell} />;
    case "guardian_warning": return <GuardianWarningCell cell={cell} />;

    case "context_compaction":
      return <ContextCompactionCell cell={cell} />;

    case "thinking":
      return <ThinkingCell cell={cell} isStreaming={cell.isStreaming || isActive} conversationId={conversationId} workspaceRoot={workspaceRoot} knownFilePaths={knownFilePaths} />;

    case "collaboration":
      return <CollaborationCell cell={cell} conversationId={conversationId} isTranscriptMode={isTranscriptMode} />;

    case "activity":
      if (conversationId && cell.toolCallRecords?.some(record => record.mcpApp)) return <>
        {cell.toolCallRecords.map(record => record.mcpApp
          ? <McpAppActivity key={record.id} cell={cell} record={record} conversationId={conversationId}
              workspaceRoot={workspaceRoot} readOnly={isTranscriptMode} />
          : <ActivityCell key={record.id} cell={{ ...cell, id: record.id, toolCallRecords: [record], outputText: undefined }}
              conversationId={conversationId} workspaceRoot={workspaceRoot} />)}
      </>;
      return <ActivityCell cell={cell} conversationId={conversationId} workspaceRoot={workspaceRoot} />;

    case "exec":
      return <ExecCell cell={cell} isActive={isActive} conversationId={conversationId} allowGuardianApproval={!isTranscriptMode} />;

    case "diff":
      return <DiffCell cell={cell} allowRevert={allowFileRevert} conversationId={conversationId} workspaceRoot={workspaceRoot} />;

    case "error":
      return <ErrorCell cell={cell} />;

    case "assistant_markdown":
      return <AssistantMarkdownCell
        cell={cell}
        isTranscriptMode={isTranscriptMode}
        conversationId={conversationId}
        workspaceRoot={workspaceRoot}
        knownFilePaths={knownFilePaths}
        afterContent={afterContent}
      />;

    default:
      // Live assistant text never reaches this renderer: the turn projection
      // routes a provisional item into the work timeline as process text and
      // the settled answer into the reply area as assistant markdown.
      return null;
  }
});

function McpAppActivity({ cell, record, conversationId, workspaceRoot, readOnly }: {
  cell: ActivityCellState; record: ToolCallRecord; conversationId: string; workspaceRoot?: string; readOnly: boolean;
}) {
  return <McpAppInlineSlot app={record.mcpApp!} conversationId={conversationId} readOnly={readOnly}
    fallback={<ActivityCell cell={{ ...cell, id: record.id, toolCallRecords: [record], outputText: undefined }}
      conversationId={conversationId} workspaceRoot={workspaceRoot} />} />;
}
