import type { ChatMessage, ContentBlock } from "../stores/types";
import { getContentBlocks } from "../lib/content-blocks";
import { projectTurn, type TurnActivityItem } from "../lib/turn-projection";
import { resolveCitations } from "./citationProjection";
import type {
  ActivityCellState,
  AssistantMarkdownCellState,
  ChatSurfaceState,
  ChatTurnState,
  DiffCellState,
  ErrorCellState,
  ExecCellState,
  HistoryCellState,
  StatusNoticeCellState,
  StreamingAssistantTailCellState,
  ThinkingCellState,
  UserMessageCellState,
} from "./cells/cellTypes";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import { recordInputTarget, recordOutcomeMeta } from "./cells/activityCellHelpers";
import { purifyToolErrorText } from "./errorMessages";

type CommittedCellState = Exclude<
  HistoryCellState,
  UserMessageCellState | StreamingAssistantTailCellState
>;

const statusForActivity = (item: TurnActivityItem): ActivityCellState["status"] => {
  if (item.status === "running" || item.status === "pending") return "running";
  if (item.status === "failed" || item.status === "blocked" || item.status === "timeout") return "failed";
  if (item.status === "partial") return "partial";
  if (item.status === "cancelled") return "interrupted";
  return "done";
};

const activityTitle = (item: TurnActivityItem): string => {
  const record = item.records?.at(-1);
  const rawTitle = item.title
    || (record?.status === "running" || record?.status === "pending"
      ? record.displayHint
      : record?.displaySummary || record?.displayHint)
    || item.summary
    || item.content
    || "Activity";
  const operation = record?.displayHint || record?.name || "";
  if (
    item.kind === "fileChange"
    && record
    && (record.status === "running" || record.status === "pending")
  ) {
    const target = recordInputTarget(record);
    if (target) return `${operation || "Write"} ${target}`;
  }
  const normalized = rawTitle.match(/^(?:Completed|Failed|Blocked|Cancelled|Timed out):\s*(.+)$/i)?.[1]?.trim();
  return normalized && operation && normalized.toLowerCase() === operation.toLowerCase()
    ? operation
    : rawTitle;
};

const activitySubtitle = (item: TurnActivityItem): string => {
  const record = item.records?.at(-1);
  const candidate = (record ? recordInputTarget(record) : "") || item.summary || record?.userSummary || "";
  const outcome = record ? recordOutcomeMeta(record) : "";
  const title = activityTitle(item).trim().toLowerCase();
  const target = candidate && !title.includes(candidate.trim().toLowerCase()) ? candidate : "";
  return [target, outcome].filter(Boolean).join(" · ");
};

const thinkingSource = (item: TurnActivityItem): ThinkingCellState["source"] => {
  if (item.source === "commentary") return "commentary";
  if (item.source === "provider") return "provider";
  if (item.source === "model_preamble") return "model_preamble";
  if (item.source === "post_tool") return "post_tool";
  if (item.source === "runtime") return "runtime";
  return "reasoning";
};

const outputLines = (value: string | undefined): string[] =>
  String(value || "").split(/\r?\n/).filter(Boolean).slice(-20);

const commandFor = (record: ToolCallRecord): string => {
  const command = record.args?.command;
  return typeof command === "string" && command.trim()
    ? command
    : record.inputSummary || record.displayHint || "Command";
};

const exitCodeFor = (record: ToolCallRecord): number | undefined => {
  const raw = (record as ToolCallRecord & { exitCode?: unknown }).exitCode
    ?? (record.errorInfo as { exit_code?: unknown } | undefined)?.exit_code;
  const value = Number(raw);
  if (Number.isFinite(value)) return value;
  const summary = `${record.summary || ""}\n${record.outputPreview || ""}`;
  const parsed = summary.match(/\bExit code:\s*(-?\d+)/i)?.[1];
  if (parsed == null) return undefined;
  const exitCode = Number(parsed);
  return Number.isFinite(exitCode) ? exitCode : undefined;
};

const execCell = (record: ToolCallRecord): ExecCellState => {
  // stdoutPreview/stderrPreview are the typed command streams. outputPreview is
  // their combined compatibility preview, so using it as stdout while a typed
  // stderr stream exists renders stderr twice. Only fall back to the combined
  // preview (or the terminal summary) when no typed stream was recorded.
  const hasTypedOutput = Boolean(record.stdoutPreview || record.stderrPreview);
  const rawStdout = purifyToolErrorText(
    hasTypedOutput ? record.stdoutPreview : record.outputPreview || record.summary,
  );
  const stdout = hasTypedOutput || record.outputPreview
    ? rawStdout
    : rawStdout.replace(/^Exit code:\s*-?\d+(?:\s*\(failed\))?\s*/i, "").trimStart();
  const stderr = purifyToolErrorText(record.stderrPreview);
  return {
    kind: "exec",
    id: record.id,
    command: commandFor(record),
    cwd: typeof record.args?.cwd === "string" ? record.args.cwd : undefined,
    background: record.args?.run_in_background === true,
    status: record.status === "running" || record.status === "pending"
      ? "running"
      : record.status === "success"
        ? "success"
        : record.status === "partial"
          ? "partial"
        : record.status === "cancelled"
          ? "cancelled"
          : "failed",
    exitCode: exitCodeFor(record),
    stdoutPreview: outputLines(stdout),
    stderrPreview: outputLines(stderr),
    stdoutFull: stdout,
    stderrFull: stderr,
    durationMs: record.durationMs,
    collapsed: !Boolean(stdout || stderr),
    createdAt: record.startedAt,
    completedAt: record.finishedAt,
  };
};

const targetPath = (record: ToolCallRecord): string => {
  const value = record.args?.file_path ?? record.args?.path ?? record.args?.target ?? record.args?.filename;
  return typeof value === "string" && value.trim()
    ? value
    : record.inputSummary || record.displaySummary || "Changed file";
};

const diffCell = (items: TurnActivityItem[]): DiffCellState | null => {
  const records = items.flatMap((item) => item.records ?? []).filter((record) => record.diff);
  if (!records.length) return null;
  const files = records.flatMap((record) => {
    const structuredFiles = record.diff?.files;
    if (structuredFiles?.length) {
      return structuredFiles.map((file) => ({
        path: file.path,
        patch: file.patch,
        additions: file.plus,
        deletions: file.minus,
        changeType: file.status === "added"
          ? "created" as const
          : file.status === "deleted"
            ? "deleted" as const
            : "updated" as const,
        isLarge: file.plus + file.minus > 200,
      }));
    }
    return [{
      path: targetPath(record),
      patch: record.diff?.patch,
      additions: record.diff?.plus ?? 0,
      deletions: record.diff?.minus ?? 0,
      changeType: "updated" as const,
      isLarge: (record.diff?.plus ?? 0) + (record.diff?.minus ?? 0) > 200,
    }];
  });
  const firstItem = items[0];
  return {
    kind: "diff",
    id: `diff-${firstItem?.id || "files"}`,
    status: "updated",
    files,
    summary: {
      added: files.reduce((sum, file) => sum + file.additions, 0),
      deleted: files.reduce((sum, file) => sum + file.deletions, 0),
      modifiedFiles: files.length,
    },
    collapsed: items.every((item) => item.status === "completed"),
    createdAt: firstItem?.startedAt ?? Date.now(),
  };
};

const activityCell = (item: TurnActivityItem, message: ChatMessage): ActivityCellState => ({
  kind: "activity",
  id: item.id,
  activityKind: item.kind,
  title: activityTitle(item),
  subtitle: activitySubtitle(item) || undefined,
  status: statusForActivity(item),
  collapsed: !["running", "pending"].includes(item.status),
  toolCallRecords: item.records,
  progress: item.progress?.length
    ? { text: item.progress.at(-1)?.summary || item.progress.at(-1)?.message }
    : undefined,
  skill: item.kind === "skill"
    ? {
        name: item.skillName,
        triggerMode: item.triggerMode,
        sourceLevel: item.sourceLevel,
        reason: item.reason,
        tokenEstimate: item.tokenEstimate,
        content: item.content,
      }
    : undefined,
  startedAt: item.startedAt ?? message.timestamp,
  completedAt: item.finishedAt,
});

const processCells = (items: TurnActivityItem[], message: ChatMessage): CommittedCellState[] => {
  const cells: CommittedCellState[] = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (["reasoning", "processNote", "providerReasoning", "agentMessage"].includes(item.kind)) {
      if (!item.content) continue;
      cells.push({
        kind: "thinking",
        id: item.id,
        content: item.content,
        source: thinkingSource(item),
        isRawProviderReasoning: item.isRawProviderReasoning ?? item.kind === "providerReasoning",
        providerReasoningType: item.providerReasoningType,
        phase: item.phase,
        isStreaming: item.status === "running",
        createdAt: item.startedAt ?? message.timestamp,
      });
      continue;
    }
    if (item.kind === "commandExecution" && item.records?.length) {
      cells.push(...item.records.map(execCell));
      continue;
    }
    if (item.kind === "fileChange") {
      if (!item.records?.some((record) => record.diff)) {
        cells.push(activityCell(item, message));
        continue;
      }
      const fileItems = [item];
      while (
        index + 1 < items.length
        && items[index + 1]?.kind === "fileChange"
        && items[index + 1]?.records?.some((record) => record.diff)
      ) {
        fileItems.push(items[index + 1]);
        index += 1;
      }
      const diff = diffCell(fileItems);
      if (diff) {
        cells.push(diff);
        continue;
      }
    }
    cells.push(activityCell(item, message));
  }
  return cells;
};

const userCell = (message: ChatMessage | null): UserMessageCellState | null => message
  ? {
      kind: "user_message",
      id: message.id,
      content: message.content,
      attachments: message.attachmentRefs?.map((attachment) => ({
        id: attachment.id,
        artifactId: attachment.artifactId,
        docId: attachment.docId,
        name: attachment.name,
        type: attachment.mediaType,
        size: attachment.sizeBytes,
        dataUrl: attachment.dataUrl,
      })),
      createdAt: message.timestamp,
      queueState: message.queueState,
      queuePosition: message.queuePosition,
      queueMessageId: message.queueMessageId,
      steeredIntoMessageId: message.steeredIntoMessageId,
    }
  : null;

const liveTail = (blocks: ContentBlock[], message: ChatMessage): StreamingAssistantTailCellState | null => {
  if (!message.isStreaming) return null;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (
      block.type === "text"
      && block.content.trim()
      && block.isStreaming === true
    ) {
      return {
        kind: "streaming_assistant_tail",
        id: block.itemId || `${message.id}-live-answer`,
        partialMarkdown: block.content,
        updatedAt: Date.now(),
      };
    }
  }
  return null;
};

function buildTurn(userMessage: ChatMessage | null, assistantMessage: ChatMessage | null): ChatTurnState {
  if (!assistantMessage) {
    return {
      id: userMessage?.id ?? `turn-${Date.now()}`,
      userCell: userCell(userMessage),
      committedCells: [],
      activeCell: null,
      finalAnswerCell: null,
      status: "completed",
      startedAt: userMessage?.timestamp ?? Date.now(),
    };
  }

  const blocks = getContentBlocks(assistantMessage);
  const projection = projectTurn(blocks, {
    isStreaming: assistantMessage.isStreaming,
    isThinkingStreaming: assistantMessage.isThinkingStreaming,
    terminalStatus: assistantMessage.terminalStatus,
  });
  const finalBlock = [...blocks].reverse().find(
    (block): block is Extract<ContentBlock, { type: "text" }> =>
      block.type === "text" && block.isStreaming !== true && (
        block.status === "completed" || block.status === "partial"
      ),
  );
  const citations = resolveCitations(
    assistantMessage.citations,
    blocks,
    projection.finalAnswer,
    finalBlock?.providerRaw?.citations,
  );
  const finalAnswerCell: AssistantMarkdownCellState | null = projection.finalAnswer.trim()
    ? {
        kind: "assistant_markdown",
        id: `${assistantMessage.id}-final`,
        messageId: assistantMessage.id,
        markdownSource: projection.finalAnswer,
        citations,
        phase: "final",
        copyable: true,
        isStreaming: Boolean(assistantMessage.isStreaming && !finalBlock),
        source: projection.finalAnswerSource === "reply"
          || projection.finalAnswerSource === "partial"
          ? projection.finalAnswerSource
          : "stream",
        attachments: assistantMessage.replyAttachments,
        createdAt: assistantMessage.timestamp,
      }
    : null;
  const committedCells = processCells(projection.activityItems, assistantMessage);
  if (
    assistantMessage.terminalStatus === "failed"
    && !committedCells.some((cell) => cell.kind === "activity" && cell.status === "failed")
    && !committedCells.some((cell) => cell.kind === "exec" && cell.status === "failed")
  ) {
    const error: ErrorCellState = {
      kind: "error",
      id: `error-${assistantMessage.id}`,
      title: "Request failed",
      message: assistantMessage.failureMessage || assistantMessage.content || "The agent run failed.",
      source: "agent",
      recoverable: false,
      createdAt: assistantMessage.completedAt ?? assistantMessage.timestamp,
    };
    committedCells.push(error);
  }

  return {
    id: assistantMessage.id,
    userCell: userCell(userMessage),
    committedCells,
    activeCell: finalAnswerCell ? null : liveTail(blocks, assistantMessage),
    finalAnswerCell,
    status: assistantMessage.isStreaming
      ? "streaming"
      : assistantMessage.terminalStatus === "failed"
        ? "failed"
        : assistantMessage.terminalStatus === "partial"
          ? "partial"
          : assistantMessage.terminalStatus === "interrupted"
            ? "interrupted"
            : "completed",
    startedAt: userMessage?.timestamp ?? assistantMessage.timestamp,
    completedAt: assistantMessage.isStreaming
      ? undefined
      : assistantMessage.completedAt ?? assistantMessage.timestamp,
    durationMs: assistantMessage.durationMs,
  };
}

// Message state is immutable in the Zustand chat store. Keep the same
// projection object for unchanged historical message identities so a live
// token delta only re-renders the active turn. This follows the keyed message
// memoization used by CC/Codex transcript views without introducing a second
// display protocol or changing the turn shape.
type TurnCacheEntry = {
  userMessage: ChatMessage | null;
  assistantMessage: ChatMessage | null;
  isStreaming: boolean;
  turn: ChatTurnState;
};

const turnProjectionCache = new WeakMap<object, TurnCacheEntry[]>();

function buildTurnMemoized(
  userMessage: ChatMessage | null,
  assistantMessage: ChatMessage | null,
  isStreaming: boolean,
): ChatTurnState {
  const anchor = (assistantMessage ?? userMessage) as object | null;
  if (anchor) {
    const entries = turnProjectionCache.get(anchor) ?? [];
    const cached = entries.find(
      (entry) => entry.userMessage === userMessage
        && entry.assistantMessage === assistantMessage
        && entry.isStreaming === isStreaming,
    );
    if (cached) return cached.turn;
    const turn = buildTurn(userMessage, assistantMessage);
    if (turn.status === "streaming" && !isStreaming) turn.status = "completed";
    entries.push({ userMessage, assistantMessage, isStreaming, turn });
    // A message normally has at most two states (live and settled). Bound the
    // cache defensively if a caller toggles streaming repeatedly.
    if (entries.length > 3) entries.splice(0, entries.length - 3);
    turnProjectionCache.set(anchor, entries);
    return turn;
  }
  return buildTurn(userMessage, assistantMessage);
}

const statusNotice = (message: ChatMessage): StatusNoticeCellState => ({
  kind: "status_notice",
  id: `notice-${message.id}`,
  tone: message.failureMessage ? "danger" : "info",
  title: message.content || "System notice",
  createdAt: message.timestamp,
});

const isQuietSystemNotice = (message: ChatMessage): boolean =>
  message.role === "system" && message.id === "system-guidelines-updated";

export function projectMessagesToTurns(
  messages: ChatMessage[],
  isStreaming: boolean,
): ChatTurnState[] {
  const turns: ChatTurnState[] = [];
  let index = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (isQuietSystemNotice(message)) {
      index += 1;
      continue;
    }
    if (message.role === "user") {
      const assistant = messages[index + 1]?.role === "assistant" ? messages[index + 1] : null;
      const assistantOwnsLiveStream = Boolean(
        isStreaming && (assistant?.isStreaming || assistant?.isThinkingStreaming),
      );
      turns.push(buildTurnMemoized(message, assistant, assistantOwnsLiveStream));
      index += assistant ? 2 : 1;
      continue;
    }
    if (message.role === "assistant") {
      turns.push(buildTurnMemoized(
        null,
        message,
        Boolean(isStreaming && (message.isStreaming || message.isThinkingStreaming)),
      ));
      index += 1;
      continue;
    }
    if (message.role === "system") {
      turns.push({
        id: `notice-${message.id}`,
        userCell: null,
        committedCells: [statusNotice(message)],
        activeCell: null,
        finalAnswerCell: null,
        status: "completed",
        startedAt: message.timestamp,
      });
    }
    index += 1;
  }
  return turns;
}

const projectedTurnStarts = (messages: ChatMessage[]): number[] => {
  const starts: number[] = [];
  let index = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (isQuietSystemNotice(message)) {
      index += 1;
      continue;
    }
    if (message.role === "user") {
      starts.push(index);
      index += messages[index + 1]?.role === "assistant" ? 2 : 1;
      continue;
    }
    if (message.role === "assistant" || message.role === "system") starts.push(index);
    index += 1;
  }
  return starts;
};

export function projectRecentMessagesToTurns(
  messages: ChatMessage[],
  isStreaming: boolean,
  recentTurnLimit: number,
): { turns: ChatTurnState[]; hiddenTurnCount: number; totalTurnCount: number } {
  const starts = projectedTurnStarts(messages);
  const totalTurnCount = starts.length;
  const limit = Math.max(0, Math.floor(recentTurnLimit));
  if (!limit || totalTurnCount <= limit) {
    return {
      turns: projectMessagesToTurns(messages, isStreaming),
      hiddenTurnCount: 0,
      totalTurnCount,
    };
  }
  const hiddenTurnCount = totalTurnCount - limit;
  return {
    turns: projectMessagesToTurns(messages.slice(starts[hiddenTurnCount] ?? 0), isStreaming),
    hiddenTurnCount,
    totalTurnCount,
  };
}
