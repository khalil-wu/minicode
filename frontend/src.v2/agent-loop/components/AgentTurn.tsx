import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import type React from "react";
import type { HistoryCellState } from "../../chat/cells/cellTypes";
import type { AgentLoopTurnProjection } from "../projection/project-turn";
import { AgentProcessSummary } from "./AgentProcessSummary";
import { AgentTimeline } from "./AgentTimeline";
import { FinalAnswer } from "./FinalAnswer";
import { useTranscriptSearch } from "../../chat/TranscriptSearchContext";
import { useTranscriptReadingPreference } from "../../chat/transcriptReadingState";

export type RenderAgentCellArgs = {
  key?: React.Key;
  cell: HistoryCellState;
  isActive?: boolean;
  className?: string;
  afterContent?: React.ReactNode;
};

export type RenderAgentCell = (args: RenderAgentCellArgs) => React.ReactNode;

export const AgentTurn = memo(function AgentTurn({
  turn,
  wide = false,
  renderCell,
  defaultProcessExpanded,
  historyControl,
  loadedToolItems,
}: {
  turn: AgentLoopTurnProjection;
  wide?: boolean;
  renderCell: RenderAgentCell;
  defaultProcessExpanded?: boolean;
  historyControl?: React.ReactNode;
  loadedToolItems?: number;
}) {
  // Incomplete, interrupted, failed, or answer-less turns are evidence, not a
  // disclosure preference. They stay visible until a complete final answer
  // establishes the only valid collapse boundary for the turn.
  const initialProcessExpanded = !turn.hasCompleteFinalAnswer
    ? true
    : defaultProcessExpanded ?? turn.initialProcessExpanded;
  const [processPreference, setProcessExpanded, userToggled] = useTranscriptReadingPreference(`process:${turn.processDetailMode}`, initialProcessExpanded);
  const searching = useTranscriptSearch();
  const processExpanded = searching || processPreference;
  const previousTurnId = useRef(turn.id);
  const previousDetailMode = useRef(turn.processDetailMode);
  const previousDefaultProcessExpanded = useRef(defaultProcessExpanded);
  const previousStatus = useRef(turn.status);
  const previousHasCompleteFinalAnswer = useRef(turn.hasCompleteFinalAnswer);

  useEffect(() => {
    const changedTurn = previousTurnId.current !== turn.id;
    const changedMode = previousDetailMode.current !== turn.processDetailMode;
    const changedDefault = previousDefaultProcessExpanded.current !== defaultProcessExpanded;
    const reachedCompleteAnswer =
      !previousHasCompleteFinalAnswer.current
      && turn.hasCompleteFinalAnswer;
    const lostCompleteAnswer = previousHasCompleteFinalAnswer.current && !turn.hasCompleteFinalAnswer;
    const enteredRunning =
      previousStatus.current !== "running"
      && turn.status === "running";

    if (changedTurn || changedMode) {
      userToggled.current = false;
      setProcessExpanded(initialProcessExpanded);
    } else if (lostCompleteAnswer && !userToggled.current) {
      setProcessExpanded(true);
    } else if (changedDefault && turn.hasCompleteFinalAnswer && !userToggled.current) {
      setProcessExpanded(initialProcessExpanded);
    } else if (
      reachedCompleteAnswer
      && turn.processDetailMode !== "verbose"
      && defaultProcessExpanded !== true
      && !userToggled.current
    ) {
      setProcessExpanded(initialProcessExpanded);
    } else if (
      enteredRunning
      && turn.processDetailMode !== "verbose"
      && defaultProcessExpanded === undefined
      && !userToggled.current
    ) {
      setProcessExpanded(true);
    }

    previousTurnId.current = turn.id;
    previousDetailMode.current = turn.processDetailMode;
    previousDefaultProcessExpanded.current = defaultProcessExpanded;
    previousStatus.current = turn.status;
    previousHasCompleteFinalAnswer.current = turn.hasCompleteFinalAnswer;
  }, [
    turn.id,
    initialProcessExpanded,
    turn.processDetailMode,
    turn.status,
    turn.hasCompleteFinalAnswer,
    defaultProcessExpanded,
  ]);

  // A settled file mutation is an outcome, not another activity row. Keep it
  // in the authoritative process projection for metrics, but render it after
  // the reply so the user sees the complete change set at the end of the turn.
  const timelineCells = useMemo(() => turn.processCells.filter((cell) => cell.kind !== "diff"), [turn.processCells]);
  const diffCells = useMemo(() => turn.processCells.filter((cell) => cell.kind === "diff"), [turn.processCells]);
  const onUserDisclosure = useCallback(() => {
    userToggled.current = true;
    setProcessExpanded(true);
  }, [setProcessExpanded, userToggled]);
  const visibleTimelineCells = useMemo(() => processExpanded
    ? timelineCells
    : [], [processExpanded, timelineCells]);
  const hasTimelineItems = turn.processCells.length > 0 || Boolean(historyControl);
  const failureIsTimelineEvidence = useMemo(() => turn.processCells.some((cell) => cell.kind === "error"), [turn.processCells]);
  const showProcessStack =
    turn.hasProcessContent &&
    visibleTimelineCells.length > 0;
  const standaloneNotice = !turn.userCell && turn.status === "completed"
    && turn.processCells.length > 0
    && turn.processCells.every((cell) => cell.kind === "status_notice");
  const processSummary = standaloneNotice ? null : (
    <AgentProcessSummary
      status={turn.hasCompleteFinalAnswer && turn.status === "running" ? "completed" : turn.status}
      processExpanded={processExpanded}
      hasTimelineItems={hasTimelineItems}
      durationMs={turn.durationMs}
      startedAt={turn.startedAt}
      failureMessage={failureIsTimelineEvidence && processExpanded ? undefined : turn.failureMessage}
      canCollapse={turn.hasCompleteFinalAnswer}
      canExpand={userToggled.current && !processExpanded}
      position="top"
      onToggle={() => {
        userToggled.current = true;
        setProcessExpanded((value) => !value);
      }}
    />
  );
  const fileChanges = diffCells.length > 0 && turn.status !== "running" ? (
    <section className="chat-turn-diff-zone agent-loop-diff-area" data-zone="diff" aria-label="文件修改">
      {diffCells.map((cell) => renderCell({ key: cell.id, cell }))}
    </section>
  ) : null;
  return (
    <div
      className="chat-turn agent-loop-turn"
      data-message-id={turn.id}
      data-status={turn.status}
      style={turnStyle(wide)}
    >
      {turn.userCell && renderCell({
        cell: turn.userCell,
        className: "chat-turn-user-cell agent-loop-user-cell",
      })}

      {turn.hasProcessContent && (
        <section
          className="chat-turn-process agent-loop-process agent-loop-work-area"
          data-zone="work"
          data-active={turn.status === "running" && !turn.hasCompleteFinalAnswer ? "true" : "false"}
          data-collapsed={!processExpanded ? "true" : "false"}
          aria-label="Agent 处理进度"
          onClickCapture={(event) => {
            if ((event.target as Element).closest("button[aria-expanded], button.agent-loop-timeline-group-title, summary")) {
              userToggled.current = true;
              setProcessExpanded(processPreference);
            }
          }}
        >
          {processSummary}
          {processExpanded && historyControl}

          {showProcessStack && (
            <AgentTimeline
              loadedToolItems={loadedToolItems}
              cells={visibleTimelineCells}
              renderCell={renderCell}
              isRunning={turn.status === "running"}
              expandWorkGroups={userToggled.current}
              showAllOpenWork={turn.status !== "running" && !turn.hasCompleteFinalAnswer}
              onUserDisclosure={onUserDisclosure}
            />
          )}

        </section>
      )}

      {turn.answerCell && (
        <section
          className="chat-turn-answer-zone agent-loop-reply-area"
          data-zone="reply"
          aria-label="Agent 回复"
        >
          <FinalAnswer
            cell={turn.answerCell}
            isStreaming={turn.answerIsStreaming}
            isActive={Boolean(turn.activeAnswerCell)}
            renderCell={renderCell}
            afterContent={fileChanges}
          />
        </section>
      )}

      {!turn.answerCell && fileChanges}

    </div>
  );
});

const turnStyle = (wide: boolean): React.CSSProperties => ({
  display: "flex",
  flexDirection: "column",
  gap: 8,
  width: wide ? "var(--chat-wide-axis-width)" : "var(--chat-axis-width)",
  margin: "0 auto",
});
