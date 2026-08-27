import type { ChatMessage, ContentBlock } from "../stores/types";
import {
  getContentBlocks,
  isFinalAnswerBlock,
} from "../lib/content-blocks";
import {
  activityKindFromToolRecord,
  activityStatusFromToolRecords,
  projectTurn,
  type TurnActivityItem,
} from "../lib/turn-projection";
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
  CollaborationCellState,
} from "./cells/cellTypes";
import type { ToolCallRecord } from "../lib/tool-call-reducer";
import { recordInputTarget, recordOutcomeMeta } from "./cells/activityCellHelpers";
import { readableToolLabel } from "./toolDisplayName";
import { purifyToolErrorText } from "./errorMessages";
import { workspaceFilePathComparisonKey } from "../lib/workspace-path";

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
  const rawTitle = readableToolLabel(item.title
    || (record?.status === "running" || record?.status === "pending"
      ? record.displayHint
      : record?.displaySummary || record?.displayHint)
    || item.summary
    || item.content
    || "活动");
  const operation = readableToolLabel(record?.displayHint || record?.name);
  if (
    item.kind === "fileChange"
    && record
    && (record.status === "running" || record.status === "pending")
  ) {
    const target = recordInputTarget(record);
    if (target) return `${operation || "写入"} ${target}`;
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
  if (item.source === "pending") return "model_preamble";
  if (item.kind === "processNote" && item.source === "model") return "commentary";
  return "reasoning";
};

const outputLines = (value: string | undefined): string[] =>
  String(value || "").split(/\r?\n/).filter(Boolean).slice(-20);

const commandFor = (record: ToolCallRecord): string => {
  const command = record.args?.command;
  if (typeof command === "string" && command.trim()) return command;
  // A pending tool call may arrive before its streamed arguments. displayHint is
  // the tool's label ("Run"), not a command, and printing it in the command slot
  // claims the agent is running a command called "Run".
  return record.inputSummary || "准备命令…";
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

const execCell = (record: ToolCallRecord, item?: TurnActivityItem): ExecCellState => {
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
    // Completed commands stay as one-line process evidence. Their stdout is
    // available on demand, so test-run summaries do not flood the timeline.
    collapsed: true,
    createdAt: record.startedAt,
    completedAt: record.finishedAt,
    segment: item?.segment,
    segmentClosed: item?.segmentClosed,
  };
};

const targetPath = (record: ToolCallRecord): string => {
  const value = record.args?.file_path ?? record.args?.path ?? record.args?.target ?? record.args?.filename;
  return typeof value === "string" && value.trim()
    ? value
    : record.inputSummary || record.displaySummary || "已更改文件";
};

const diffCell = (items: TurnActivityItem[]): DiffCellState | null => {
  const records = items.flatMap((item) => item.records ?? []).filter((record) => record.diff);
  if (!records.length) return null;
  const files = records.flatMap((record) => {
    const structuredFiles = record.diff?.files;
    if (structuredFiles?.length) {
      return structuredFiles.map((file) => ({
        path: file.path,
        oldPath: file.oldPath,
        patch: file.patch,
        additions: file.plus,
        deletions: file.minus,
        changeType: file.status === "added"
          ? "created" as const
          : file.status === "deleted"
            ? "deleted" as const
            : file.status === "renamed"
              ? "renamed" as const
            : "updated" as const,
        isLarge: file.plus + file.minus > 200,
      }));
    }
    return [{
      path: targetPath(record),
      oldPath: undefined,
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
    toolCallCount: records.length,
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
  collapsed: true,
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
  segment: item.segment,
  segmentClosed: item.segmentClosed,
});

/**
 * A provider may persist several tool records under one activity envelope.
 * Reads and commands retain their own chronological rows; only consecutive
 * mutations of the same path are coalesced by the projection below.
 */
const splitActivityRecords = (item: TurnActivityItem): TurnActivityItem[] => {
  const records = item.records ?? [];
  if (records.length <= 1) return [item];
  return records.map((record, index) => ({
    ...item,
    id: `${item.id}-${record.id || index}`,
    kind: activityKindFromToolRecord(record),
    records: [record],
    status: activityStatusFromToolRecords([record]),
    title: record.displaySummary || record.displayHint || item.title,
    summary: record.inputSummary || record.userSummary || item.summary,
    startedAt: record.startedAt ?? item.startedAt,
    finishedAt: record.finishedAt ?? item.finishedAt,
  }));
};

/** Consecutive edits to one path are one user-facing operation. Every tool
 * record stays in the cell so its accumulated stats and patch remain exact. */
const coalesceConsecutiveFileChanges = (
  items: TurnActivityItem[],
  workspaceRoot: string,
): TurnActivityItem[] => {
  const grouped: TurnActivityItem[] = [];
  for (const item of items) {
    if (item.kind !== "fileChange" || !item.records?.length) {
      grouped.push(item);
      continue;
    }
    const previous = grouped.at(-1);
    const pathKey = workspaceFilePathComparisonKey(recordInputTarget(item.records[0]), workspaceRoot);
    const previousPathKey = previous?.kind === "fileChange" && previous.records?.length
      ? workspaceFilePathComparisonKey(recordInputTarget(previous.records[0]), workspaceRoot)
      : "";
    if (!pathKey || !previous || previous.kind !== "fileChange" || pathKey !== previousPathKey) {
      grouped.push(item);
      continue;
    }
    const records = [...(previous.records ?? []), ...item.records];
    const status = activityStatusFromToolRecords(records);
    grouped[grouped.length - 1] = {
      ...previous,
      blocks: [...previous.blocks, ...item.blocks],
      records,
      status,
      title: item.title || previous.title,
      summary: item.summary || previous.summary,
      startedAt: Math.min(previous.startedAt ?? Infinity, item.startedAt ?? Infinity),
      finishedAt: status === "running" || status === "pending"
        ? undefined
        : Math.max(previous.finishedAt ?? 0, item.finishedAt ?? 0) || undefined,
      durationMs: (previous.durationMs ?? 0) + (item.durationMs ?? 0) || undefined,
      hasFailure: previous.hasFailure || item.hasFailure,
      hasPendingUserAction: previous.hasPendingUserAction || item.hasPendingUserAction,
    };
  }
  return grouped;
};

const processCells = (
  items: TurnActivityItem[],
  message: ChatMessage,
  workspaceRoot: string,
): CommittedCellState[] => {
  const cells: CommittedCellState[] = [];
  const diffCells: DiffCellState[] = [];
  const normalizedItems = coalesceConsecutiveFileChanges(
    items.flatMap(splitActivityRecords),
    workspaceRoot,
  );
  for (let index = 0; index < normalizedItems.length; index += 1) {
    const item = normalizedItems[index];
    if (item.source === "runtime") continue;
    if (["reasoning", "processNote", "providerReasoning", "agentMessage"].includes(item.kind)) {
      if (!item.content) continue;
      cells.push({
        kind: "thinking",
        id: item.id,
        content: item.content,
        source: thinkingSource(item),
        phase: item.phase,
        isStreaming: item.status === "running",
        createdAt: item.startedAt ?? message.timestamp,
        segment: item.segment,
        segmentClosed: item.segmentClosed,
      });
      continue;
    }
    if (item.kind === "commandExecution" && item.records?.length) {
      cells.push(...item.records.map((record) => execCell(record, item)));
      continue;
    }
    const collaboration = collaborationCells(item);
    if (collaboration.length > 0) {
      cells.push(...collaboration.map((cell) => ({
        ...cell,
        segment: item.segment,
        segmentClosed: item.segmentClosed,
      })));
      continue;
    }
    if (item.kind === "fileChange") {
      // Keep the authoritative edit/write lifecycle in the process trace for
      // every terminal state. The aggregate diff below is a separate outcome
      // projection, so removing this row would make the process history lie
      // about which mutation calls actually ran.
      cells.push(activityCell(item, message));
      const diff = diffCell([item]);
      if (diff) diffCells.push(diff);
      continue;
    }
    cells.push(activityCell(item, message));
  }
  const overallDiff = aggregateDiffCells(diffCells, message.id, workspaceRoot);
  return overallDiff ? [...cells, overallDiff] : cells;
};

/** One final process-trace diff contains the complete set of file mutations. */
const aggregateDiffCells = (
  diffCells: DiffCellState[],
  messageId: string,
  workspaceRoot: string,
): DiffCellState | null => {
  if (diffCells.length === 0) return null;
  const filesByPath = new Map<string, DiffCellState["files"][number]>();
  for (const cell of diffCells) {
    for (const file of cell.files) {
      const key = workspaceFilePathComparisonKey(file.path, workspaceRoot);
      const previous = filesByPath.get(key);
      if (!previous) {
        filesByPath.set(key, { ...file });
        continue;
      }
      filesByPath.set(key, {
        ...previous,
        ...file,
        // A tool record's patch is a snapshot for that mutation, not a
        // concatenable hunk stream. Keep the newest snapshot here; the
        // turn-owned diff event supplies the authoritative net patch when it
        // is available.
        patch: file.patch || previous.patch,
        additions: previous.additions + file.additions,
        deletions: previous.deletions + file.deletions,
        isLarge: Boolean(previous.isLarge || file.isLarge),
        isTruncated: Boolean(previous.isTruncated || file.isTruncated),
      });
    }
  }
  const files = [...filesByPath.values()];
  return {
    kind: "diff",
    id: `diff-${messageId}-files`,
    status: "updated",
    files,
    summary: {
      added: files.reduce((sum, file) => sum + file.additions, 0),
      deleted: files.reduce((sum, file) => sum + file.deletions, 0),
      modifiedFiles: files.length,
    },
    toolCallCount: diffCells.reduce((sum, cell) => sum + (cell.toolCallCount ?? 0), 0),
    collapsed: false,
    createdAt: Math.min(...diffCells.map((cell) => cell.createdAt)),
  };
};

const collaborationCells = (item: TurnActivityItem): CollaborationCellState[] => {
  const cells: CollaborationCellState[] = [];
  for (const record of item.records ?? []) {
    const name = String(record.name || "").trim();
    if (name === "send_message") {
      const recipient = stringArg(record.args?.recipient) || "子智能体";
      const content = stringArg(record.args?.message);
      if (!content) continue;
      cells.push(collaborationCell(record, "sent_message", [{
        agentId: recipient,
        agentLabel: collaborationAgentLabel(recipient),
        content,
      }]));
      continue;
    }
    if (name === "task_stop") {
      const target = stringArg(record.args?.subagent_id);
      if (!target) continue;
      cells.push(collaborationCell(record, "closed", [{
        agentId: target,
        agentLabel: collaborationAgentLabel(target),
      }]));
      continue;
    }
    if (name !== "task") continue;
    // TaskTool's actual triad-aligned batch field is ``parallel_tasks``.
    // ``subtasks`` never exists on the production schema and made the live
    // renderer miss successful parallel delegations despite unit tests passing.
    const rawSubtasks = record.args?.parallel_tasks;
    const rawEntries = Array.isArray(rawSubtasks) && rawSubtasks.length > 0
      ? rawSubtasks
      : [record.args];
    const entries = rawEntries.flatMap((raw, index) => {
      if (!raw || typeof raw !== "object") return [];
      const args = raw as Record<string, unknown>;
      const content = stringArg(args.prompt);
      if (!content) return [];
      const explicitId = taskResultAgentIds(record)[index];
      const fallback = stringArg(args.description) || stringArg(args.agent_type) || `任务 ${index + 1}`;
      const agentId = explicitId || fallback;
      return [{ agentId, agentLabel: collaborationAgentLabel(agentId), content }];
    });
    if (entries.length > 0) cells.push(collaborationCell(record, "sent_message", entries));
  }
  return cells;
};

const collaborationCell = (
  record: ToolCallRecord,
  action: CollaborationCellState["action"],
  entries: CollaborationCellState["entries"],
): CollaborationCellState => ({
  kind: "collaboration",
  id: `collaboration-${record.id}`,
  action,
  status: record.status === "running" || record.status === "pending"
    ? "running"
    : record.status === "success"
      ? "success"
      : "failed",
  entries,
  collapsed: false,
  createdAt: record.startedAt ?? record.finishedAt,
});

const stringArg = (value: unknown): string => typeof value === "string" ? value.trim() : "";

const taskResultAgentIds = (record: ToolCallRecord): string[] => {
  const value = `${record.outputPreview || ""}\n${record.summary || ""}`;
  return [...value.matchAll(/\bsubagent-[a-z0-9]+\b/gi)].map((match) => match[0]);
};

const collaborationAgentLabel = (value: string): string => {
  const label = value.trim();
  if (!/^subagent-[a-z0-9]+$/i.test(label)) return label;
  return label.slice("subagent-".length) || label;
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

const liveTail = (
  blocks: ContentBlock[],
  message: ChatMessage,
): StreamingAssistantTailCellState | null => {
  if (!message.isStreaming) return null;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (
      block.type === "text"
      && block.isStreaming === true
      && block.content.trim()
      && !["pending", "commentary", "model_preamble", "post_tool", "runtime"].includes(String(block.source || ""))
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

function buildTurn(
  userMessage: ChatMessage | null,
  assistantMessage: ChatMessage | null,
  workspaceRoot: string,
  userTurnIsStreaming = false,
): ChatTurnState {
  if (!assistantMessage) {
    return {
      id: userMessage?.id ?? `turn-${Date.now()}`,
      turnId: userMessage?.turnId,
      userCell: userCell(userMessage),
      committedCells: [],
      activeCell: null,
      finalAnswerCell: null,
      status: userTurnIsStreaming ? "streaming" : "completed",
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
      block.type === "text" && isFinalAnswerBlock(block),
  );
  const citations = resolveCitations(
    assistantMessage.citations,
    blocks,
    projection.finalAnswer,
    finalBlock?.providerRaw?.citations,
  );
  const imageProgress = blocks.filter(
    (block): block is Extract<ContentBlock, { type: "progress" }> =>
      block.type === "progress" && block.stage === "image_generation",
  );
  const artifacts = assistantMessage.artifacts ?? [];
  const imageTextSplit = splitAnswerAroundImageArtifact(
    blocks,
    projection.finalAnswer,
    imageProgress.length > 0,
    artifacts,
  );
  const hasAnswerProjection = Boolean(
    projection.finalAnswer.trim() || artifacts.length || imageProgress.length,
  );
  const finalAnswerCell: AssistantMarkdownCellState | null = hasAnswerProjection
    ? {
        kind: "assistant_markdown",
        id: `${assistantMessage.id}-final`,
        messageId: assistantMessage.id,
        markdownSource: projection.finalAnswer,
        markdownBeforeArtifacts: imageTextSplit.before,
        markdownAfterArtifacts: imageTextSplit.after,
        citations,
        phase: "final",
        copyable: true,
        isStreaming: Boolean(assistantMessage.isStreaming && !finalBlock),
        source: projection.finalAnswerSource === "reply"
          || projection.finalAnswerSource === "partial"
          ? projection.finalAnswerSource
          : "stream",
        attachments: assistantMessage.replyAttachments,
        artifacts,
        imageProgress,
        failureMessage: assistantMessage.failureMessage,
        failureRecoverable: assistantMessage.failureRecoverable,
        createdAt: assistantMessage.timestamp,
      }
    : null;
  const committedCells = processCells(
    projection.activityItems.filter((item) =>
      !item.progress?.some((progress) => progress.stage === "image_generation"),
    ),
    assistantMessage,
    workspaceRoot,
  );
  if (
    assistantMessage.terminalStatus === "failed"
    && !imageProgress.some((progress) => progress.status === "failed")
    && !committedCells.some((cell) => cell.kind === "activity" && cell.status === "failed")
    && !committedCells.some((cell) => cell.kind === "exec" && cell.status === "failed")
  ) {
    const error: ErrorCellState = {
      kind: "error",
      id: `error-${assistantMessage.id}`,
      title: "请求失败",
      message: assistantMessage.failureMessage || assistantMessage.content || "Agent 运行失败。",
      source: "agent",
      recoverable: assistantMessage.failureRecoverable ?? false,
      createdAt: assistantMessage.completedAt ?? assistantMessage.timestamp,
    };
    committedCells.push(error);
  }

  return {
    id: assistantMessage.id,
    turnId: assistantMessage.turnId,
    userCell: userCell(userMessage),
    committedCells,
    activeCell: liveTail(blocks, assistantMessage),
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
      : assistantMessage.completedAt,
    durationMs: assistantMessage.durationMs,
    usage: assistantMessage.usage,
  };
}

function splitAnswerAroundImageArtifact(
  blocks: ContentBlock[],
  fullAnswer: string,
  hasImageProgress: boolean,
  artifacts: ChatMessage["artifacts"],
): { before: string; after: string } {
  const anchoredOffset = artifacts
    .filter((artifact) => artifact.kind === "image")
    .map((artifact) => artifact.textOffset)
    .filter((offset): offset is number => typeof offset === "number" && Number.isFinite(offset))
    .sort((left, right) => left - right)[0];
  if (anchoredOffset !== undefined) {
    const offset = Math.max(0, Math.min(fullAnswer.length, Math.trunc(anchoredOffset)));
    return {
      before: fullAnswer.slice(0, offset),
      after: fullAnswer.slice(offset),
    };
  }
  if (!hasImageProgress) return { before: fullAnswer, after: "" };
  const progressIndex = blocks.findIndex((block) => block.type === "progress" && block.stage === "image_generation");
  if (progressIndex < 0) return { before: fullAnswer, after: "" };
  const answerBlocks = blocks.filter(
    (block): block is Extract<ContentBlock, { type: "text" }> =>
      block.type === "text" && isFinalAnswerBlock(block),
  );
  const before = answerBlocks
    .filter((block) => {
      const blockIndex = blocks.indexOf(block);
      return blockIndex < progressIndex;
    })
    .map((block) => block.content)
    .join("");
  const after = answerBlocks
    .filter((block) => {
      const blockIndex = blocks.indexOf(block);
      return blockIndex > progressIndex;
    })
    .map((block) => block.content)
    .join("");
  if (before && after) return { before, after };

  // Older transcripts persisted the completed provider text as one merged
  // block before the image progress block, so block ordering alone places the
  // artifact after the completion sentence. The Images adapter owns this
  // fixed completion copy, which makes it a safe compatibility anchor for
  // artifacts written before durable textOffset support existed.
  if (artifacts.some((artifact) => artifact.kind === "image")) {
    const legacyCompletion = "图像已经为你生成好了。";
    const legacyCompletionOffset = fullAnswer.lastIndexOf(legacyCompletion);
    if (legacyCompletionOffset > 0) {
      return {
        before: fullAnswer.slice(0, legacyCompletionOffset),
        after: fullAnswer.slice(legacyCompletionOffset),
      };
    }
  }

  return before || after ? { before, after } : { before: fullAnswer, after: "" };
}

// Message state is immutable in the Zustand chat store. Keep the same
// projection object for unchanged historical message identities so a live
// token delta only re-renders the active turn. This follows the keyed message
// memoization used by MiniCode transcript views without introducing a second
// display protocol or changing the turn shape.
type TurnCacheEntry = {
  userMessage: ChatMessage | null;
  assistantMessage: ChatMessage | null;
  isStreaming: boolean;
  workspaceRoot: string;
  turn: ChatTurnState;
};

const turnProjectionCache = new WeakMap<object, TurnCacheEntry[]>();

function buildTurnMemoized(
  userMessage: ChatMessage | null,
  assistantMessage: ChatMessage | null,
  isStreaming: boolean,
  workspaceRoot: string,
): ChatTurnState {
  const anchor = (assistantMessage ?? userMessage) as object | null;
  if (anchor) {
    const entries = turnProjectionCache.get(anchor) ?? [];
    const cached = entries.find(
      (entry) => entry.userMessage === userMessage
        && entry.assistantMessage === assistantMessage
        && entry.isStreaming === isStreaming
        && entry.workspaceRoot === workspaceRoot,
    );
    if (cached) return cached.turn;
    const turn = buildTurn(userMessage, assistantMessage, workspaceRoot, isStreaming && !assistantMessage);
    if (turn.status === "streaming" && !isStreaming) turn.status = "completed";
    entries.push({ userMessage, assistantMessage, isStreaming, workspaceRoot, turn });
    // A message normally has at most two states (live and settled). Bound the
    // cache defensively if a caller toggles streaming repeatedly.
    if (entries.length > 3) entries.splice(0, entries.length - 3);
    turnProjectionCache.set(anchor, entries);
    return turn;
  }
  return buildTurn(userMessage, assistantMessage, workspaceRoot, isStreaming && !assistantMessage);
}

const statusNotice = (message: ChatMessage): StatusNoticeCellState => ({
  kind: "status_notice",
  id: `notice-${message.id}`,
  tone: message.failureMessage ? "danger" : "info",
  title: message.systemNoticeTitle || (message.failureMessage ? "运行失败" : "系统通知"),
  message: message.content || message.failureMessage || undefined,
  createdAt: message.timestamp,
});

const isQuietSystemNotice = (message: ChatMessage): boolean =>
  message.role === "system" && message.id === "system-guidelines-updated";

const isHiddenTimelineMessage = (message: ChatMessage): boolean =>
  message.queueState === "queued" || isQuietSystemNotice(message);

export function projectMessagesToTurns(
  messages: ChatMessage[],
  isStreaming: boolean,
  workspaceRoot = "",
): ChatTurnState[] {
  const turns: ChatTurnState[] = [];
  let index = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (isHiddenTimelineMessage(message)) {
      index += 1;
      continue;
    }
    if (message.role === "user") {
      const nextMessage = messages[index + 1];
      const assistant = nextMessage?.role === "assistant" && !isHiddenTimelineMessage(nextMessage)
        ? nextMessage
        : null;
      const turnOwnsLiveStream = Boolean(
        isStreaming && (
          !assistant
          || assistant.isStreaming
          || assistant.isThinkingStreaming
        ),
      );
      turns.push(buildTurnMemoized(message, assistant, turnOwnsLiveStream, workspaceRoot));
      index += assistant ? 2 : 1;
      continue;
    }
    if (message.role === "assistant") {
      turns.push(buildTurnMemoized(
        null,
        message,
        Boolean(isStreaming && (message.isStreaming || message.isThinkingStreaming)),
        workspaceRoot,
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
    if (isHiddenTimelineMessage(message)) {
      index += 1;
      continue;
    }
    if (message.role === "user") {
      starts.push(index);
      const nextMessage = messages[index + 1];
      index += nextMessage?.role === "assistant" && !isHiddenTimelineMessage(nextMessage) ? 2 : 1;
      continue;
    }
    if (message.role === "assistant" || message.role === "system") starts.push(index);
    index += 1;
  }
  return starts;
};

type TopologyAnchor = {
  index: number;
  message: ChatMessage;
};

export interface RecentTurnProjectionCache {
  cacheKey: string;
  recentTurnLimit: number;
  messageCount: number;
  sourceMessages: ChatMessage[] | null;
  firstMessage: ChatMessage | null;
  windowStart: number;
  prefixBoundary: ChatMessage | null;
  prefixAnchors: TopologyAnchor[];
  recentTopology: string[];
  totalTurnCount: number;
}

export const createRecentTurnProjectionCache = (): RecentTurnProjectionCache => ({
  cacheKey: "",
  recentTurnLimit: 0,
  messageCount: 0,
  sourceMessages: null,
  firstMessage: null,
  windowStart: 0,
  prefixBoundary: null,
  prefixAnchors: [],
  recentTopology: [],
  totalTurnCount: 0,
});

const messageTopology = (message: ChatMessage | undefined): string => {
  if (!message) return "";
  return [
    message.id,
    message.role,
    message.queueState ?? "",
    isQuietSystemNotice(message) ? "quiet" : "visible",
  ].join("\u0000");
};

const prefixTopologyAnchors = (
  messages: ChatMessage[],
  windowStart: number,
): TopologyAnchor[] => {
  if (windowStart <= 1) return [];
  // A transcript reload/rewind replaces the full prefix and is caught by the
  // first/boundary guards.  These evenly distributed identities additionally
  // invalidate a cache when an older middle record is replaced while the
  // recent streaming tail remains unchanged, without re-walking thousands of
  // historical messages for every token.
  const anchorCount = Math.min(32, windowStart - 1);
  const anchors: TopologyAnchor[] = [];
  for (let offset = 1; offset <= anchorCount; offset += 1) {
    const index = Math.floor((offset * (windowStart - 1)) / (anchorCount + 1));
    const message = messages[index];
    if (message && !anchors.some((anchor) => anchor.index === index)) {
      anchors.push({ index, message });
    }
  }
  return anchors;
};

const canReuseRecentTurnProjection = (
  cache: RecentTurnProjectionCache,
  messages: ChatMessage[],
  isStreaming: boolean,
  recentTurnLimit: number,
  cacheKey: string,
): boolean => {
  if (
    cache.cacheKey !== cacheKey
    || cache.recentTurnLimit !== recentTurnLimit
    || cache.totalTurnCount <= recentTurnLimit
    || cache.messageCount !== messages.length
    || cache.windowStart < 0
    || cache.windowStart >= messages.length
    || cache.firstMessage !== (messages[0] ?? null)
  ) return false;
  if (cache.sourceMessages === messages) return true;
  // Outside a live stream, transcript mutations are infrequent and should be
  // rebuilt exactly.  The fast path is reserved for the high-frequency case
  // where only the current assistant record is replaced per delta.
  if (!isStreaming) return false;
  if (cache.windowStart > 0 && cache.prefixBoundary !== messages[cache.windowStart - 1]) {
    return false;
  }
  if (cache.prefixAnchors.some((anchor) => messages[anchor.index] !== anchor.message)) {
    return false;
  }
  if (cache.recentTopology.length !== messages.length - cache.windowStart) return false;
  for (let index = cache.windowStart; index < messages.length; index += 1) {
    if (cache.recentTopology[index - cache.windowStart] !== messageTopology(messages[index])) {
      return false;
    }
  }
  return true;
};

export function projectRecentMessagesToTurns(
  messages: ChatMessage[],
  isStreaming: boolean,
  recentTurnLimit: number,
  cache?: RecentTurnProjectionCache,
  cacheKey = "",
  workspaceRoot = "",
): { turns: ChatTurnState[]; hiddenTurnCount: number; totalTurnCount: number } {
  const limit = Math.max(0, Math.floor(recentTurnLimit));
  if (cache && limit > 0 && canReuseRecentTurnProjection(
    cache,
    messages,
    isStreaming,
    limit,
    cacheKey,
  )) {
    cache.sourceMessages = messages;
    const hiddenTurnCount = cache.totalTurnCount - limit;
    return {
      turns: projectMessagesToTurns(messages.slice(cache.windowStart), isStreaming, workspaceRoot),
      hiddenTurnCount,
      totalTurnCount: cache.totalTurnCount,
    };
  }

  const starts = projectedTurnStarts(messages);
  const totalTurnCount = starts.length;
  if (!limit || totalTurnCount <= limit) {
    if (cache) Object.assign(cache, createRecentTurnProjectionCache(), {
      cacheKey,
      recentTurnLimit: limit,
      messageCount: messages.length,
      sourceMessages: messages,
      firstMessage: messages[0] ?? null,
      totalTurnCount,
    });
    return {
      turns: projectMessagesToTurns(messages, isStreaming, workspaceRoot),
      hiddenTurnCount: 0,
      totalTurnCount,
    };
  }
  const hiddenTurnCount = totalTurnCount - limit;
  const windowStart = starts[hiddenTurnCount] ?? 0;
  if (cache) Object.assign(cache, {
    cacheKey,
    recentTurnLimit: limit,
    messageCount: messages.length,
    sourceMessages: messages,
    firstMessage: messages[0] ?? null,
    windowStart,
    prefixBoundary: windowStart > 0 ? messages[windowStart - 1] : null,
    prefixAnchors: prefixTopologyAnchors(messages, windowStart),
    recentTopology: messages.slice(windowStart).map(messageTopology),
    totalTurnCount,
  });
  return {
    turns: projectMessagesToTurns(messages.slice(windowStart), isStreaming, workspaceRoot),
    hiddenTurnCount,
    totalTurnCount,
  };
}
