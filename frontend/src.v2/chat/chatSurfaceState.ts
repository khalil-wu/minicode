import type { ArtifactPreview, ChatMessage, ContentBlock } from "../stores/types";
import { projectSourceAssistantText } from "../lib/source-assistant-text";
import { acknowledgeMessageUpdate, messageTopologyKey, streamingMessageUpdate } from "../lib/message-changes";
import {
  getContentBlocks,
  getPresentedContentBlocks,
  isFinalAnswerBlock,
  isLegacyTextBlock,
} from "../lib/content-blocks";
import {
  activityKindFromToolRecord,
  activityStatusFromToolRecords,
  projectProgressBlock,
  projectToolBlock,
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
  ContextCompactionCellState,
  StreamingAssistantTailCellState,
  ThinkingCellState,
  UserMessageCellState,
  CollaborationCellState,
  TurnWorkItem,
} from "./cells/cellTypes";
import { isToolCallAwaitingApproval, isToolCallExecuting, type ToolCallRecord } from "../lib/tool-call-reducer";
import { shallow } from "zustand/shallow";
import { getRecordOutputText, knownFilePathsForCell, recordInputTarget, recordOutcomeMeta, taskResultAgentIds } from "./cells/activityCellHelpers";
import { readableToolLabel } from "./toolDisplayName";
import { purifyToolErrorText } from "./errorMessages";
import { workspaceFilePathComparisonKey } from "../lib/workspace-path";
import { canonicalArtifactKind, isExecutionResultArtifact, recordHasImageArtifact } from "../lib/artifact-projection";
import { applyAuthoritativeTurnDiff } from "../lib/turn-diff";

type CommittedCellState = Exclude<
  HistoryCellState,
  UserMessageCellState | StreamingAssistantTailCellState
>;

const statusForActivity = (item: TurnActivityItem): ActivityCellState["status"] => {
  if (item.status === "running" || item.status === "pending") {
    if (item.records?.some(isToolCallExecuting)) return "running";
    if (item.records?.some(isToolCallAwaitingApproval)) return "pending_approval";
    return item.records?.length ? "pending" : item.status;
  }
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
  const normalized = readableToolLabel(rawTitle.match(/^(?:Completed|Failed|Blocked|Cancelled|Timed out):\s*(.+)$/i)?.[1]);
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
  if (item.collapsible) return "commentary";
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
  if (record.commandPresentation) return record.commandPresentation.command;
  const command = record.args?.command;
  if (typeof command === "string" && command.trim()) return command;
  // A pending tool call may arrive before its streamed arguments. displayHint is
  // the tool's label ("Run"), not a command, and printing it in the command slot
  // claims the agent is running a command called "Run".
  return record.inputSummary || "";
};

const exitCodeFor = (record: ToolCallRecord): number | undefined => {
  const raw = (record as ToolCallRecord & { exitCode?: unknown }).exitCode
    ?? (record.errorInfo as { exit_code?: unknown } | undefined)?.exit_code;
  const value = Number(raw);
  if (raw != null && Number.isFinite(value)) return value;
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
  const hasTypedOutput = record.stdoutPreview !== undefined || record.stderrPreview !== undefined;
  // A native approval/cancellation receipt is model context, not OS stdout.
  // Its original summary/error remains on the canonical/raw record.
  const nativeBeforeExecutionTerminal = record.commandPresentation && record.executionStartedAt == null
    && record.status !== "pending" && record.status !== "running";
  const rawStdout = hasTypedOutput
    ? record.stdoutPreview || ""
    : nativeBeforeExecutionTerminal ? "" : getRecordOutputText(record);
  const stdout = hasTypedOutput || record.outputPreview
    ? rawStdout
    : rawStdout.replace(/^Exit code:\s*-?\d+(?:\s*\(failed\))?\s*/i, "").trimStart();
  const stderr = record.stderrPreview || "";
  return {
    kind: "exec",
    id: record.id,
    command: commandFor(record),
    commandActions: record.commandPresentation && record.executionStartedAt == null
      && record.status !== "pending" && record.status !== "running" ? undefined : record.commandActions,
    commandPresentation: record.commandPresentation,
    executionStartedAt: record.executionStartedAt,
    callSource: record.callSource,
    cwd: record.commandPresentation?.cwd ?? (typeof record.args?.cwd === "string" ? record.args.cwd : undefined),
    background: record.args?.run_in_background === true,
    // Source's planned commandExecution is inProgress before approval. The
    // record still retains waiting_approval and the actual execution clock.
    status: record.commandPresentation && (record.status === "running" || record.status === "pending")
      ? "running"
      : record.status === "running" || record.status === "pending"
      ? isToolCallAwaitingApproval(record) ? "pending_approval" : isToolCallExecuting(record) ? "running" : "pending"
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
    outputArtifact: record.artifactId && !recordHasImageArtifact(record) ? {
      artifactId: record.artifactId,
      kind: canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record),
      summary: "完整命令输出",
      mediaType: record.artifactMediaType || "text/plain",
      bytes: record.artifactBytes,
    } : undefined,
    durationMs: record.commandPresentation && record.executionStartedAt == null ? undefined : record.durationMs,
    cleanupReceipt: record.cleanupReceipt,
    needsApproval: isToolCallAwaitingApproval(record),
    // Completed commands stay as one-line process evidence. Their stdout is
    // available on demand, so test-run summaries do not flood the timeline.
    collapsed: true,
    createdAt: record.startedAt,
    completedAt: record.finishedAt,
    segment: item?.segment,
    segmentClosed: item?.segmentClosed,
  };
};

const execCells = (record: ToolCallRecord, item?: TurnActivityItem): ExecCellState[] => {
  const execution = execCell(record, item);
  const actions = execution.commandActions;
  if (!actions || actions.length < 2) return [execution];
  // One canonical command item can describe several parsed shell actions.
  // Like Codex, each action references the same execution/output receipt.
  return actions.map((action, index) => ({
    ...execution,
    id: `${record.id}:${index}`,
    commandExecutionItemId: record.id,
    originalCommand: execution.command,
    command: action.command,
    commandActions: [action],
  }));
};

const targetPath = (record: ToolCallRecord): string => {
  const value = record.args?.file_path ?? record.args?.path ?? record.args?.target ?? record.args?.filename;
  return typeof value === "string" && value.trim()
    ? value
    : record.inputSummary || record.displaySummary || "已更改文件";
};

const diffCell = (items: TurnActivityItem[]): DiffCellState | null => {
  const records = items.flatMap((item) => item.records ?? []).filter((record) => record.diff && record.status === "success");
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
  outputText: item.outputText,
  progress: item.progress?.length
    ? {
        text: item.progress.at(-1)?.summary || item.progress.at(-1)?.message,
        phase: item.progress.at(-1)?.phase,
        retryAttempt: item.progress.at(-1)?.retryAttempt,
        maxRetries: item.progress.at(-1)?.maxRetries,
        retryAfterMs: item.progress.at(-1)?.retryAfterMs,
        providerState: item.progress.at(-1)?.providerState,
        errorMessage: item.progress.at(-1)?.errorMessage,
        detail: item.progress.at(-1)?.detail,
      }
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
 * Every tool call retains its own chronological row and mutation patch.
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

type ProcessItemProjection = { cells: CommittedCellState[]; diff?: DiffCellState | null };
type ProcessItemCacheEntry = {
  item: TurnActivityItem;
  messageId: string;
  timestamp: number;
  workspaceRoot: string;
  images: ArtifactPreview[];
  projection: ProcessItemProjection;
};

// Store updates retain completed block/record identities. Cache by that source,
// not the assistant message, which is replaced for every text delta.
const processItemCache = new WeakMap<ContentBlock, Map<string, ProcessItemCacheEntry>>();
const sameProcessItem = (previous: TurnActivityItem, next: TurnActivityItem): boolean => (
  Object.keys(previous).length === Object.keys(next).length
  && (Object.keys(next) as (keyof TurnActivityItem)[]).every((key) => {
    if (key === "blocks" || key === "records" || key === "progress") return shallow(previous[key], next[key]);
    return Object.is(previous[key], next[key]);
  })
);

const projectProcessItem = (
  item: TurnActivityItem,
  message: ChatMessage,
): ProcessItemProjection => {
  if (item.kind === "guardianReview" && item.review) return { cells: [{ kind: "guardian_review", id: item.id,
    review: item.review, status: item.status === "running" ? "running" : "done", createdAt: item.startedAt ?? message.timestamp,
    segment: item.segment, segmentClosed: item.segmentClosed }] };
  if (item.kind === "guardianWarning") return { cells: [{ kind: "guardian_warning", id: item.id, message: item.content ?? "", createdAt: message.timestamp }] };
  if (item.kind === "contextCompaction") return { cells: [{ kind: "context_compaction", id: item.id,
    status: item.status === "running" ? "running" : item.status === "cancelled" ? "interrupted" : "completed",
    source: item.source, createdAt: item.startedAt ?? message.timestamp, completedAt: item.finishedAt }] };
  if (item.source === "runtime") return { cells: [] };
  if (["reasoning", "processNote", "providerReasoning", "agentMessage"].includes(item.kind)) {
    if (!item.content) return { cells: [] };
    return { cells: [{
      kind: "thinking",
      id: item.id,
      content: item.content,
      source: thinkingSource(item),
      phase: item.phase,
      providerReasoningType: item.providerReasoningType,
      collapsible: item.collapsible,
      isStreaming: item.status === "running",
      createdAt: item.startedAt ?? message.timestamp,
      segment: item.segment,
      segmentClosed: item.segmentClosed,
    }] };
  }
  if (item.outputText !== undefined) return { cells: [activityCell(item, message)] };
  if (item.records?.length && (item.kind === "commandExecution" || item.records.every((record) => record.commandActions?.length))) {
    return { cells: item.records.flatMap((record) => execCells(record.errorKind === "approval_rejected"
      && item.automaticApprovalReviews?.some(review => review.status === "denied")
      ? { ...record, commandActions: undefined } : record, item)) };
  }
  const collaboration = collaborationCells(item);
  if (collaboration.length > 0) {
    const rows: CommittedCellState[] = collaboration.map((cell) => ({
      ...cell,
      segment: item.segment,
      segmentClosed: item.segmentClosed,
    }));
    if (item.records?.some((record) => ["failed", "blocked", "timeout", "partial", "cancelled"].includes(record.status))) rows.push(activityCell(item, message));
    return { cells: rows };
  }
  if (item.kind === "fileChange") {
    // Keep the authoritative edit/write lifecycle in the process trace for
    // every terminal state. The aggregate diff below is a separate outcome
    // projection, so removing this row would make the process history lie
    // about which mutation calls actually ran.
    return { cells: [activityCell(item, message)], diff: diffCell([item]) };
  }
  return { cells: [activityCell(item, message)] };
};

const processCells = (
  items: TurnActivityItem[],
  message: ChatMessage,
  workspaceRoot: string,
  toolImages: ArtifactPreview[],
): CommittedCellState[] => {
  const cells: CommittedCellState[] = [];
  const diffCells: DiffCellState[] = [];
  const imagesByCall = new Map<string, ArtifactPreview[]>();
  for (const artifact of toolImages) {
    if (artifact.toolCallId) imagesByCall.set(artifact.toolCallId, [...(imagesByCall.get(artifact.toolCallId) ?? []), artifact]);
  }
  const normalizedItems = items.flatMap(splitActivityRecords);
  for (const item of normalizedItems) {
    const anchor = item.blocks[0];
    const entries = processItemCache.get(anchor) ?? new Map<string, ProcessItemCacheEntry>();
    const cached = entries.get(item.id);
    const images = (item.records ?? []).flatMap((record) => imagesByCall.get(record.id) ?? []);
    const cacheMatches = cached && cached.messageId === message.id && cached.timestamp === message.timestamp
      && cached.workspaceRoot === workspaceRoot && sameProcessItem(cached.item, item) && shallow(cached.images, images);
    const projection = cacheMatches ? cached.projection : projectProcessItem(item, message);
    if (!cacheMatches && images.length) projection.cells = projection.cells.map((cell) => {
      const ownedImages = cell.kind === "activity"
        ? (cell.toolCallRecords ?? []).flatMap((record) => imagesByCall.get(record.id) ?? [])
        : cell.kind === "exec" ? imagesByCall.get(cell.commandExecutionItemId ?? cell.id) ?? []
        : [];
      return ownedImages.length && (cell.kind === "activity" || cell.kind === "exec") ? { ...cell, artifacts: ownedImages } : cell;
    });
    entries.set(item.id, { item, messageId: message.id, timestamp: message.timestamp, workspaceRoot, images, projection });
    processItemCache.set(anchor, entries);
    cells.push(...projection.cells.map(cell => {
      if (!item.automaticApprovalReviews || (cell.kind !== "exec" && cell.kind !== "activity")) return cell;
      const guardianDeclined = item.records?.some(record => record.errorKind === "approval_rejected")
        && item.automaticApprovalReviews.some(review => review.status === "denied");
      const withReviews = { ...cell, automaticApprovalReviews: item.automaticApprovalReviews, guardianDeclined,
        ...(cell.kind === "activity" && item.guardianPatchFiles ? { guardianPatchFiles: item.guardianPatchFiles } : {}) };
      // An approval rejection never created shell stdout. Retain the original
      // model-facing receipt in its record; the actual review owns this detail.
      return cell.kind === "exec" && guardianDeclined ? { ...withReviews,
        commandActions: undefined, stdoutPreview: [], stderrPreview: [], stdoutFull: "", stderrFull: "" } : withReviews;
    }));
    if (projection.diff) diffCells.push(projection.diff);
  }
  const overallDiff = aggregateDiffCells(diffCells, message.id, workspaceRoot);
  const projectedImageIds = new Set(cells.flatMap((cell) => cell.kind === "activity" || cell.kind === "exec" ? (cell.artifacts ?? []).map((artifact) => artifact.artifactId) : []));
  for (const artifact of toolImages) {
    if (projectedImageIds.has(artifact.artifactId)) continue;
    // Tool pages keep their exact owner id even before the owning step loads.
    if (artifact.toolCallId && message.toolPage?.remaining) continue;
    cells.push({
      kind: "activity", id: `${message.id}-image-${artifact.artifactId}`,
      activityKind: artifact.operation?.startsWith("browser") ? "browser" : artifact.operation === "tool_exec" || artifact.operation === "tool_wait" ? "commandExecution" : "mcpToolCall",
      title: artifact.operation?.startsWith("browser") ? "浏览器截图" : artifact.operation === "tool_exec" || artifact.operation === "tool_wait" ? "代码执行输出" : artifact.summary,
      status: "done", collapsed: true, artifacts: [artifact], startedAt: message.timestamp,
    });
  }
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
  for (const progress of item.progress ?? []) {
    if (!progress.subagentId) continue;
    cells.push({
      kind: "collaboration", id: progress.id, action: "completed",
      status: progress.subagentStatus === "cancelled" ? "cancelled" : progress.status === "failed" ? "failed" : progress.status === "partial" ? "partial" : "success",
      entries: [{ agentId: progress.subagentId, agentLabel: progress.subagentName || progress.label || "子智能体", agentIdentity: progress.subagentIdentity, agentStatus: progress.subagentStatus }],
      collapsed: true, createdAt: progress.timestamp,
    });
  }
  for (const record of item.records ?? []) {
    const name = String(record.name || "").trim();
    // An unsuccessful coordination request is an actual failed tool action,
    // not a child identity or a successful start. ActivityCell retains its
    // original result in the same expandable action row.
    if (["failed", "blocked", "timeout", "partial", "cancelled"].includes(record.status)
      && !(["task", "spawn_agent"].includes(name) && record.delegations?.some((delegation) => delegation.started_at > 0))) continue;
    if (name === "send_message" || name === "followup_task") {
      const requestedRecipient = stringArg(name === "followup_task" ? record.args?.target : record.args?.target ?? record.args?.recipient);
      const parentMailbox = ["parent", "team-lead"].includes(requestedRecipient);
      const recipients = parentMailbox ? [requestedRecipient]
        : record.recipientIds ?? (record.recipientId ? [record.recipientId] : requestedRecipient ? [requestedRecipient] : []);
      const content = (record.args.message as string | undefined) ?? "";
      cells.push(collaborationCell(record, "sent_message", recipients.map((recipient) => ({
        agentId: recipient,
        agentLabel: collaborationAgentLabel(recipient),
        content,
      }))));
      continue;
    }
    if (name === "task_stop" || name === "interrupt_agent") {
      const target = stringArg(name === "interrupt_agent" ? record.args?.target : record.args?.subagent_id) || "子智能体";
      cells.push(collaborationCell(record, "closed", [{
        agentId: target,
        agentLabel: collaborationAgentLabel(target),
        ...(record.args.reason ? { content: record.args.reason as string } : {}),
      }]));
      continue;
    }
    if (name !== "task" && name !== "spawn_agent") continue;
    if (record.delegations) {
      const entries = record.delegations.filter(delegation => delegation.started_at > 0).map(delegation => ({
        agentId: delegation.agent_id, agentLabel: delegation.name,
        agentIdentity: delegation.agent_path,
        agentStartedAt: delegation.started_at,
        agentStatus: delegation.status === "completed" ? "done" as const
          : delegation.status === "failed" ? "error" as const
          : delegation.status === "cancelled" || delegation.status === "interrupted" ? "cancelled" as const
          : delegation.status === "partial" ? "partial" as const
          : delegation.status === "blocked" ? "blocked" as const : "running" as const,
      }));
      if (entries.length) cells.push(collaborationCell(record, "delegated", entries));
      continue;
    }
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
      const content = (args.prompt as string | undefined) ?? "";
      const explicitId = taskResultAgentIds(record)[index];
      const fallback = stringArg(args.description) || stringArg(args.agent_type) || `任务 ${index + 1}`;
      const agentId = explicitId || fallback;
      const labels = [...new Set([stringArg(args.name), stringArg(args.description)].filter(Boolean))];
      return [{ agentId, agentLabel: labels.join(" · ") || stringArg(args.agent_type) || collaborationAgentLabel(agentId), content }];
    });
    if (entries.length > 0) cells.push(collaborationCell(record, "delegated", entries));
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
  background: record.args.run_in_background === true,
  status: record.status === "running" || record.status === "pending"
    ? "running"
    : record.status === "success"
      ? "success"
      : record.status === "partial" || record.status === "cancelled"
        ? record.status
      : "failed",
  entries,
  error: ["failed", "blocked", "timeout"].includes(record.status)
    ? getRecordOutputText(record) || record.errorInfo?.user_message || record.errorInfo?.user_summary || record.userSummary
    : undefined,
  collapsed: false,
  createdAt: record.startedAt ?? record.finishedAt,
});

const stringArg = (value: unknown): string => typeof value === "string" ? value.trim() : "";

const collaborationAgentLabel = (value: string): string => {
  const label = value.trim();
  if (label === "parent") return "主智能体";
  if (label === "*" || label === "all") return "所有智能体";
  if (/^(?:(?:subagent|agent|thread|call|session|run)[-_:][\w-]+|[a-f0-9]{8,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i.test(label)) return "Agent";
  return label.split("@")[0];
};

const userCell = (message: ChatMessage | null): UserMessageCellState | null => message
  ? {
      kind: "user_message",
      id: message.id,
      content: message.content,
      contextRefs: message.contextRefs,
      quotedMessage: message.quotedMessage,
      messageSource: message.messageSource,
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
      && !["pending", "retained", "commentary", "model_preamble", "post_tool", "runtime"].includes(String(block.source || ""))
    ) {
      const partialMarkdown = projectSourceAssistantText(block.content, block.source === "model_final" || block.source === "reply");
      if (!partialMarkdown.trim()) continue;
      return {
        kind: "streaming_assistant_tail",
        id: block.itemId || `${message.id}-live-answer`,
        partialMarkdown,
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
  inputMessages: ReadonlyMap<string, ChatMessage> = EMPTY_INPUT_MESSAGES,
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
      startedAt: undefined,
    };
  }

  const rawBlocks = getContentBlocks(assistantMessage);
  const blocks = getPresentedContentBlocks(assistantMessage, rawBlocks);
  const presentedTerminalStatus = assistantMessage.terminalStatus
    ?? (blocks.some((block, index) => block !== rawBlocks[index]) ? "partial" : undefined);
  const projection = projectTurn(blocks, {
    isStreaming: assistantMessage.isStreaming,
    isThinkingStreaming: assistantMessage.isThinkingStreaming,
    terminalStatus: presentedTerminalStatus,
  });
  const hasTypedActivity = blocks.some((block) => block.type === "tool_call" || block.type === "progress");
  const finalBlock = [...blocks].reverse().find(
    (block): block is Extract<ContentBlock, { type: "text" }> =>
      block.type === "text"
      && (isFinalAnswerBlock(block) || (hasTypedActivity && isLegacyTextBlock(block) && !block.source)),
  );
  const citations = resolveCitations(
    assistantMessage.citations,
    projection.finalAnswer,
    finalBlock?.providerRaw?.citations,
  );
  const imageProgress = blocks.filter(
    (block): block is Extract<ContentBlock, { type: "progress" }> =>
      block.type === "progress" && block.stage === "image_generation",
  );
  const records = blocks.flatMap((block) => block.type === "tool_call" ? [block.record] : []);
  const artifactOwners = new Map(records.filter((record) => record.artifactId).map((record) => [record.artifactId, record]));
  const toolImages = (assistantMessage.artifacts ?? []).flatMap((artifact) => {
    if (canonicalArtifactKind(artifact.kind, artifact.mediaType) !== "image" || artifact.source === "image_generation") return [];
    const owner = artifactOwners.get(artifact.artifactId);
    if (artifact.source !== "tool" && (!owner || owner.resultKind === "image_generation")) return [];
    if (artifact.source === "tool" && (!owner || artifact.toolCallId === owner.id)) return [artifact];
    return [{ ...artifact, source: "tool" as const, toolCallId: owner?.id || artifact.toolCallId,
      callSource: owner?.callSource || artifact.callSource, operation: owner?.name || artifact.operation }];
  });
  const toolImageIds = new Set(toolImages.map((artifact) => artifact.artifactId));
  const artifacts = (assistantMessage.artifacts ?? []).filter((artifact) => !toolImageIds.has(artifact.artifactId)
    && !(artifactOwners.has(artifact.artifactId) && isExecutionResultArtifact(artifactOwners.get(artifact.artifactId)!)));
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
    toolImages,
  );
  if (
    (assistantMessage.terminalStatus === "failed"
      || (assistantMessage.terminalStatus === "partial" && Boolean(assistantMessage.failureMessage)))
    // A failed tool is a separate item. It cannot account for a later model
    // failure unless it carries that exact terminal message.
    && !committedCells.some((cell) => {
      const failure = assistantMessage.failureMessage?.trim();
      if (!failure) return false;
      if (cell.kind === "error") return cell.message.trim() === failure;
      if (cell.kind === "exec" && cell.status === "failed") return cell.stderrFull?.trim() === failure || cell.stdoutFull?.trim() === failure;
      return cell.kind === "activity" && cell.status === "failed" && (
        cell.progress?.errorMessage?.trim() === failure
        || cell.toolCallRecords?.some((record) => [record.userSummary, record.errorInfo?.user_message, record.errorInfo?.user_summary, getRecordOutputText(record)].some((text) => text?.trim() === failure))
      );
    })
    && !imageProgress.some((progress) => progress.status === "failed" && progress.errorMessage?.trim() === assistantMessage.failureMessage?.trim())
  ) {
    const error: ErrorCellState = {
      kind: "error",
      id: `error-${assistantMessage.id}`,
      title: assistantMessage.terminalStatus === "partial" ? "响应未完整结束" : "请求失败",
      message: assistantMessage.failureMessage || assistantMessage.content || "Agent 运行失败。",
      source: "agent",
      recoverable: assistantMessage.failureRecoverable ?? false,
      createdAt: assistantMessage.completedAt ?? assistantMessage.timestamp,
    };
    committedCells.push(error);
  }

  const turn: ChatTurnState = {
    asyncMessages: blocks.filter((block): block is Extract<ContentBlock, { type: "text" }> => block.type === "text" && block.delivery === "async" && block.status === "completed"),
    id: assistantMessage.id,
    resourceKey: {},
    toolPage: assistantMessage.toolPage,
    turnId: assistantMessage.turnId,
    userCell: userCell(userMessage),
    committedCells: assistantMessage.toolPage?.remaining ? committedCells.filter((cell) => cell.kind !== "diff") : committedCells,
    activeCell: liveTail(blocks, assistantMessage),
    finalAnswerCell,
    status: assistantMessage.isStreaming
      ? "streaming"
      : presentedTerminalStatus === "failed"
        ? "failed"
        : presentedTerminalStatus === "partial"
          ? "partial"
          : presentedTerminalStatus === "interrupted"
            ? "interrupted"
            : "completed",
    startedAt: assistantMessage.runStartedAt,
    completedAt: assistantMessage.isStreaming
      ? undefined
      : assistantMessage.completedAt,
    durationMs: assistantMessage.durationMs,
    usage: assistantMessage.usage,
  };
  if (projection.userInputs?.length) {
    const workItems: TurnWorkItem[] = [];
    let segmentStart = 0;
    const workActivity = projection.activityItems.filter((item) =>
      !item.progress?.some((progress) => progress.stage === "image_generation"),
    );
    const workToolIds = new Set(workActivity.flatMap((item) => item.records?.map((record) => record.id) ?? []));
    const appendProcess = (segmentEnd: number | undefined, id: string) => {
      const items = workActivity.filter((item) => (item.segment ?? 0) >= segmentStart
        && (segmentEnd === undefined || (item.segment ?? 0) < segmentEnd));
      const toolIds = new Set(items.flatMap((item) => item.records?.map((record) => record.id) ?? []));
      const images = toolImages.filter((image) => image.toolCallId && workToolIds.has(image.toolCallId)
        ? toolIds.has(image.toolCallId) : segmentEnd === undefined);
      const cells = processCells(items, assistantMessage, workspaceRoot, images).filter((cell) => cell.kind !== "diff");
      if (segmentEnd === undefined) cells.push(...committedCells.filter((cell): cell is ErrorCellState => cell.kind === "error"
        && !cells.some((item) => item.id === cell.id)
        && !workItems.some((item) => item.kind === "process" && item.cells.some((existing) => existing.id === cell.id))));
      if (cells.length) workItems.push({ kind: "process", id, cells });
    };
    for (const input of projection.userInputs) {
      appendProcess(input.segment, `before-input:${input.userMessageId}`);
      const message = inputMessages.get(input.userMessageId);
      const cell = userCell(message ?? null);
      if (cell) workItems.push({ kind: "user", id: input.userMessageId, cell });
      segmentStart = input.segment;
    }
    appendProcess(undefined, `after-input:${projection.userInputs.at(-1)!.userMessageId}`);
    turn.workItems = workItems;
  }
  return applyAuthoritativeTurnDiff([turn], assistantMessage.turnDiff)[0];
}

function splitAnswerAroundImageArtifact(
  blocks: ContentBlock[],
  fullAnswer: string,
  hasImageProgress: boolean,
  artifacts: ChatMessage["artifacts"],
): { before: string; after: string } {
  const anchoredOffset = artifacts
    .filter((artifact) => canonicalArtifactKind(artifact.kind, artifact.mediaType) === "image")
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
  if (artifacts.some((artifact) => canonicalArtifactKind(artifact.kind, artifact.mediaType) === "image")) {
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
  inputMessages: ChatMessage[];
};

const EMPTY_INPUT_MESSAGES: ReadonlyMap<string, ChatMessage> = new Map();
const EMPTY_INPUT_ROWS: ChatMessage[] = [];

const referencedInputMessages = (messages: ChatMessage[]): Map<string, ChatMessage> => {
  const ids = new Set(messages.flatMap((message) => message.role === "assistant"
    ? getContentBlocks(message).flatMap((block) => block.type === "user_input" ? [block.userMessageId] : []) : []));
  return new Map(messages.filter((message) => message.role === "user" && ids.has(message.id)).map((message) => [message.id, message]));
};

const turnProjectionCache = new WeakMap<object, TurnCacheEntry[]>();
const cellPositions = new WeakMap<CommittedCellState[], Map<string, number>>();

function positionsForCells(cells: CommittedCellState[]): Map<string, number> {
  let positions = cellPositions.get(cells);
  if (!positions) {
    positions = new Map();
    cells.forEach((cell, index) => positions!.set(cell.id, positions!.has(cell.id) ? -1 : index));
    cellPositions.set(cells, positions);
  }
  return positions;
}

function updateTurnCells(base: ChatTurnState, message: ChatMessage): ChatTurnState | null {
  const update = streamingMessageUpdate(message)!;
  const positions = positionsForCells(base.committedCells);
  let committedCells = base.committedCells;
  let activeCell = base.activeCell;
  let resourceKey = base.resourceKey;
  const replaceCell = (index: number, cell: CommittedCellState) => {
    if (committedCells === base.committedCells) committedCells = committedCells.slice();
    committedCells[index] = cell;
  };
  for (const [blockIndex, kind] of update.changes) {
    const block = message.blocks![blockIndex];
    if (kind === "thinking" && block.type === "thinking") {
      const index = positions.get(`thinking-${blockIndex}`);
      if (index === -1) return null;
      if (index !== undefined) replaceCell(index, { ...base.committedCells[index], content: block.content } as ThinkingCellState);
    } else if (kind === "progress" && block.type === "progress") {
      const index = positions.get(block.id);
      if (index === -1) return null;
      if (index === undefined && block.status === "failed" && block.errorMessage?.trim()) return null;
      if (index !== undefined) {
        const previous = base.committedCells[index];
        if (previous.kind !== "activity") return null;
        replaceCell(index, { ...activityCell(projectProgressBlock(block), message), segment: previous.segment, segmentClosed: previous.segmentClosed });
      }
    } else if ((kind === "tool_output" || kind === "tool") && block.type === "tool_call") {
      if (["tool_exec", "tool_wait"].includes(block.record.name)) return null;
      // The single-index path cannot update one-to-many action cells.
      if ((block.record.commandActions?.length ?? 0) > 1) return null;
      const index = positions.get(block.record.id);
      // Collaboration may project several rows, and file changes own an
      // aggregate diff. Those use the full projection with its grouping rules.
      if (index === undefined || index < 0) return null;
      const previous = base.committedCells[index];
      if (previous.kind !== "activity" && previous.kind !== "exec") return null;
      // Images belong to the current message's tool ownership projection.
      // A result/stdout update must rebuild that projection, including images
      // that were added or removed since the previously rendered tool cell.
      if (previous.artifacts?.length || message.artifacts?.some((artifact) =>
        artifact.source !== "image_generation" && canonicalArtifactKind(artifact.kind, artifact.mediaType) === "image"
        && (artifact.toolCallId === block.record.id || artifact.artifactId === block.record.artifactId))) return null;
      const item = { ...projectToolBlock(block, previous.segment ?? 0), segmentClosed: previous.segmentClosed };
      const projected = projectProcessItem(item, message);
      if (projected.diff || projected.cells.length !== 1 || projected.cells[0].id !== previous.id) return null;
      replaceCell(index, projected.cells[0]);
      if (kind === "tool" && !shallow(knownFilePathsForCell(previous), knownFilePathsForCell(projected.cells[0]))) resourceKey = {};
    } else if (kind === "text" && block.type === "text") {
      if (block.delivery === "async") return null;
      const index = positions.get(block.itemId || "");
      if (index === -1) return null;
      if (index !== undefined) {
        if (base.committedCells[index].kind !== "thinking") return null;
        replaceCell(index, { ...base.committedCells[index], content: block.content } as ThinkingCellState);
      }
      activeCell = liveTail([block], message) ?? activeCell;
    }
  }
  cellPositions.set(committedCells, positions);
  return { ...base, committedCells, activeCell, resourceKey };
}

function buildTurnMemoized(
  userMessage: ChatMessage | null,
  assistantMessage: ChatMessage | null,
  isStreaming: boolean,
  workspaceRoot: string,
  inputMessages: ReadonlyMap<string, ChatMessage> = EMPTY_INPUT_MESSAGES,
): ChatTurnState {
  const inputRows = assistantMessage
    ? getContentBlocks(assistantMessage).flatMap((block) => block.type === "user_input" && inputMessages.has(block.userMessageId)
      ? [inputMessages.get(block.userMessageId)!] : []) : EMPTY_INPUT_ROWS;
  const hasInputBoundary = Boolean(assistantMessage && getContentBlocks(assistantMessage).some((block) => block.type === "user_input"));
  const anchor = (assistantMessage ?? userMessage) as object | null;
  if (anchor) {
    const entries = turnProjectionCache.get(anchor) ?? [];
    const cached = entries.find(
      (entry) => entry.userMessage === userMessage
        && entry.assistantMessage === assistantMessage
        && entry.isStreaming === isStreaming
        && entry.workspaceRoot === workspaceRoot
        && entry.inputMessages.length === inputRows.length
        && entry.inputMessages.every((message, index) => message === inputRows[index]),
    );
    if (cached) return cached.turn;
    const update = assistantMessage && streamingMessageUpdate(assistantMessage);
    const incremental = update && assistantMessage && isStreaming && !hasInputBoundary
      ? updateTurnCells(buildTurnMemoized(userMessage, update.base, isStreaming, workspaceRoot), assistantMessage)
      : null;
    const turn = incremental ?? buildTurn(userMessage, assistantMessage, workspaceRoot, isStreaming && !assistantMessage, inputMessages);
    if (turn.status === "streaming" && !isStreaming) turn.status = "completed";
    entries.push({ userMessage, assistantMessage, isStreaming, workspaceRoot, turn, inputMessages: inputRows });
    // A message normally has at most two states (live and settled). Bound the
    // cache defensively if a caller toggles streaming repeatedly.
    if (entries.length > 3) entries.splice(0, entries.length - 3);
    turnProjectionCache.set(anchor, entries);
    // The cached turn now owns this result. The next receipt starts here,
    // rather than replaying every changed tool since the start of the turn.
    if (assistantMessage) acknowledgeMessageUpdate(assistantMessage);
    return turn;
  }
  return buildTurn(userMessage, assistantMessage, workspaceRoot, isStreaming && !assistantMessage, inputMessages);
}

const statusNotice = (message: ChatMessage): StatusNoticeCellState | ContextCompactionCellState => {
  const compaction = message.blocks?.find(block => block.type === "process" && block.itemKind === "context_compaction");
  if (compaction?.type === "process") return { kind: "context_compaction", id: compaction.id,
    status: compaction.status === "running" ? "running" : compaction.status === "interrupted" ? "interrupted" : "completed",
    source: compaction.source, createdAt: compaction.startedAt ?? compaction.timestamp, completedAt: compaction.finishedAt };
  return {
  kind: "status_notice",
  id: `notice-${message.id}`,
  tone: message.failureMessage ? "danger" : "info",
  title: message.systemNoticeTitle || (message.failureMessage ? "运行失败" : "系统通知"),
  message: message.content || message.failureMessage || undefined,
  processing: message.systemNoticeProcessing,
  createdAt: message.timestamp,
  };
};

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
  const inputMessages = referencedInputMessages(messages);
  const rows = inputMessages.size ? messages.filter((message) => !inputMessages.has(message.id)) : messages;
  let latestVisibleIndex = rows.length - 1;
  while (latestVisibleIndex >= 0 && isHiddenTimelineMessage(rows[latestVisibleIndex])) latestVisibleIndex -= 1;
  let index = 0;
  while (index < rows.length) {
    const message = rows[index];
    if (isHiddenTimelineMessage(message)) {
      index += 1;
      continue;
    }
    if (message.role === "user") {
      const nextMessage = rows[index + 1];
      const assistant = nextMessage?.role === "assistant" && !isHiddenTimelineMessage(nextMessage)
        ? nextMessage
        : null;
      const turnOwnsLiveStream = Boolean(
        isStreaming && (assistant
          ? assistant.isStreaming || assistant.isThinkingStreaming
          : index === latestVisibleIndex && !message.queueState && !message.steeredIntoMessageId),
      );
      turns.push(buildTurnMemoized(message, assistant, turnOwnsLiveStream, workspaceRoot, inputMessages));
      index += assistant ? 2 : 1;
      continue;
    }
    if (message.role === "assistant") {
      turns.push(buildTurnMemoized(
        null,
        message,
        Boolean(isStreaming && (message.isStreaming || message.isThinkingStreaming)),
        workspaceRoot,
        inputMessages,
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
  const inputMessages = referencedInputMessages(messages);
  const rows = messages.map((message, index) => ({ message, index })).filter((row) => !inputMessages.has(row.message.id));
  let index = 0;
  while (index < rows.length) {
    const message = rows[index].message;
    if (isHiddenTimelineMessage(message)) {
      index += 1;
      continue;
    }
    if (message.role === "user") {
      starts.push(rows[index].index);
      const nextMessage = rows[index + 1]?.message;
      index += nextMessage?.role === "assistant" && !isHiddenTimelineMessage(nextMessage) ? 2 : 1;
      continue;
    }
    if (message.role === "assistant" || message.role === "system") starts.push(rows[index].index);
    index += 1;
  }
  return starts;
};

export interface RecentTurnProjectionCache {
  cacheKey: string;
  recentTurnLimit: number;
  messageCount: number;
  sourceMessages: ChatMessage[] | null;
  topologyKey: object | null;
  windowStart: number;
  totalTurnCount: number;
}

export const createRecentTurnProjectionCache = (): RecentTurnProjectionCache => ({
  cacheKey: "",
  recentTurnLimit: 0,
  messageCount: 0,
  sourceMessages: null,
  topologyKey: null,
  windowStart: 0,
  totalTurnCount: 0,
});

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
  ) return false;
  if (cache.sourceMessages === messages) return true;
  return cache.topologyKey === messageTopologyKey(messages);
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
      topologyKey: messageTopologyKey(messages),
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
    topologyKey: messageTopologyKey(messages),
    windowStart,
    totalTurnCount,
  });
  return {
    turns: projectMessagesToTurns(messages.slice(windowStart), isStreaming, workspaceRoot),
    hiddenTurnCount,
    totalTurnCount,
  };
}
