import type { ToolCallEvent, ToolErrorInfo, ToolResultEvent } from "../protocol/events";

export type ToolCallStatus = "pending" | "running" | "success" | "failed" | "blocked" | "partial" | "timeout" | "cancelled";

export const isTerminalToolCallStatus = (status: ToolCallStatus): boolean =>
  status !== "pending" && status !== "running";

export interface ToolCallRecord {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  /** Backend-owned lifecycle phase; status remains the stable UI terminal/running state. */
  transition?: string;
  waitingOn?: string;
  blockingReason?: string;
  summary?: string;
  artifactId?: string;
  artifactKind?: "file" | "diff" | "image" | "json" | "code" | "text" | string;
  artifactMediaType?: string;
  artifactBytes?: number;
  sourceUrl?: string;
  extractionStatus?: string;
  contentPreview?: string;
  evidenceType?: string;
  displaySummary?: string;
  resultKind?: string;
  activityKind?: string;
  visibility?: "timeline" | "compact" | "debug" | string;
  limitation?: string;
  provider?: string;
  providerErrorType?: string;
  errorInfo?: ToolErrorInfo;
  errorKind?: string;
  userSummary?: string;
  developerDetail?: string;
  recoverable?: boolean;
  projection?: string;
  durationMs?: number;
  displayHint?: string;
  inputSummary?: string;
  groupId?: string;
  stepId?: string;
  taskId?: string;
  turnId?: string;
  seq?: number;
  /** Number of explicit scope migrations accepted for this lifecycle. */
  scopeMigrationCount?: number;
  iterationId?: string;
  phase?: string;
  startedAt: number;
  finishedAt?: number;
  outputPreview?: string;
  stdoutPreview?: string;
  stderrPreview?: string;
  diff?: {
    plus: number;
    minus: number;
    patch?: string;
    files?: Array<{
      path: string;
      oldPath?: string;
      plus: number;
      minus: number;
      patch?: string;
      status?: string;
    }>;
  };
  outputFiles?: Array<{
    path: string;
    name?: string;
    size: number;
    mimeType?: string;
    isImage?: boolean;
  }>;
  cleanupReceipt?: Record<string, unknown>;
  supersededToolCallIds?: string[];
  removedFilePaths?: string[];
  /** True when a file created by this call was later removed in the same turn. */
  temporaryRemoved?: boolean;
}

const TOOL_STATUS_RANK: Record<ToolCallStatus, number> = {
  pending: 0,
  running: 1,
  partial: 2,
  failed: 3,
  blocked: 3,
  timeout: 3,
  cancelled: 3,
  success: 4,
};

/**
 * Merge a result without allowing an old terminal frame to reopen or replace
 * a newer terminal state.  A later, explicitly sequenced success may promote
 * a provisional/failed result; an unsequenced late frame is diagnostic-only.
 */
export const mergeToolCallResultRecord = (
  existing: ToolCallRecord,
  incoming: ToolCallRecord,
): ToolCallRecord => {
  const existingSeq = typeof existing.seq === "number" && Number.isFinite(existing.seq)
    ? existing.seq
    : undefined;
  const incomingSeq = typeof incoming.seq === "number" && Number.isFinite(incoming.seq)
    ? incoming.seq
    : undefined;
  const stale = existingSeq !== undefined && incomingSeq !== undefined && incomingSeq <= existingSeq;
  const existingTerminal = isTerminalToolCallStatus(existing.status);
  const incomingTerminal = isTerminalToolCallStatus(incoming.status);
  if (stale || (existingTerminal && !incomingTerminal)) return existing;
  if (existingTerminal && incomingTerminal && incomingSeq === undefined) {
    // The transport does not provide an ordering fence for this frame. Keep
    // the first terminal status and only fill fields that were previously
    // absent, so a delayed failed event cannot overwrite a confirmed success.
    return {
      ...existing,
      summary: existing.summary ?? incoming.summary,
      artifactId: existing.artifactId ?? incoming.artifactId,
      artifactKind: existing.artifactKind ?? incoming.artifactKind,
      artifactMediaType: existing.artifactMediaType ?? incoming.artifactMediaType,
      artifactBytes: existing.artifactBytes ?? incoming.artifactBytes,
      outputPreview: existing.outputPreview ?? incoming.outputPreview,
      stdoutPreview: existing.stdoutPreview ?? incoming.stdoutPreview,
      stderrPreview: existing.stderrPreview ?? incoming.stderrPreview,
      diff: existing.diff ?? incoming.diff,
      finishedAt: existing.finishedAt ?? incoming.finishedAt,
    };
  }
  if (existingTerminal && incomingTerminal
    && TOOL_STATUS_RANK[incoming.status] < TOOL_STATUS_RANK[existing.status]) {
    return existing;
  }
  return incoming;
};

const normalizedProjectionValue = (value: string | undefined): string => String(value || "").trim().toLowerCase();

export const isCommandToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.activityKind) === "commandexecution"
  || normalizedProjectionValue(record.resultKind) === "command"
  // Legacy tool_call events predate result/activity projection metadata, but
  // command_output_chunk can still arrive without an id. The executable tool
  // name is the only safe compatibility discriminator in that shape.
  || normalizedProjectionValue(record.name) === "run_command";

export const isFileChangeToolRecord = (record: ToolCallRecord): boolean =>
  !record.temporaryRemoved && (
  normalizedProjectionValue(record.activityKind) === "filechange"
  || normalizedProjectionValue(record.resultKind) === "edit");

export const isWorkspaceSearchToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.activityKind) === "workspacesearch"
  && normalizedProjectionValue(record.name) !== "list_files";

export const isWorkspaceListToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.activityKind) === "workspacelist"
  || normalizedProjectionValue(record.name) === "list_files";

export const isFileReadToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.activityKind) === "fileread"
  || normalizedProjectionValue(record.resultKind) === "file";

export const isWebFetchToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.resultKind) === "web";

export const isWebSearchToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.resultKind) === "search"
  && normalizedProjectionValue(record.activityKind) === "websearch";

export const isBrowserToolRecord = (record: ToolCallRecord): boolean => {
  const resultKind = normalizedProjectionValue(record.resultKind);
  return resultKind === "browser" || resultKind === "preview";
};

const toFiniteNumber = (value: unknown): number => {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
};

export interface DiffBadge {
  plus: number;
  minus: number;
}

export const countUnifiedDiffLines = (patch: string): DiffBadge => {
  let plus = 0;
  let minus = 0;
  let sawHunk = false;
  let inHunk = false;
  const fallbackLines = patch.split(/\r?\n/);

  for (const line of fallbackLines) {
    if (line.startsWith("diff --git ") || line.startsWith("Index: ")) {
      inHunk = false;
      continue;
    }
    if (line.startsWith("@@")) {
      sawHunk = true;
      inHunk = true;
      continue;
    }
    if (!inHunk || line.startsWith("\\ No newline")) continue;
    if (line.startsWith("+")) plus += 1;
    else if (line.startsWith("-")) minus += 1;
  }

  if (sawHunk) return { plus, minus };

  plus = 0;
  minus = 0;
  for (const line of fallbackLines) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) plus += 1;
    else if (line.startsWith("-")) minus += 1;
  }
  return { plus, minus };
};

export const getToolDiffStats = (diff: NonNullable<ToolCallRecord["diff"]>): DiffBadge => {
  if (diff.plus || diff.minus || !diff.patch) {
    return { plus: diff.plus, minus: diff.minus };
  }
  const patchStats = countUnifiedDiffLines(diff.patch);
  return patchStats.plus || patchStats.minus
    ? patchStats
    : { plus: diff.plus, minus: diff.minus };
};

export const normalizeToolDiff = (value: unknown): ToolCallRecord["diff"] | undefined => {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;

  const directPlus = toFiniteNumber(raw.plus ?? raw.additions);
  const directMinus = toFiniteNumber(raw.minus ?? raw.deletions);
  const directPatch = typeof raw.patch === "string" ? raw.patch : undefined;
  const stats = raw.stats && typeof raw.stats === "object" ? raw.stats as Record<string, unknown> : {};
  let plus = directPlus || toFiniteNumber(stats.additions);
  let minus = directMinus || toFiniteNumber(stats.deletions);
  const files = Array.isArray(raw.files) ? raw.files.filter((item): item is Record<string, unknown> =>
    Boolean(item && typeof item === "object")
  ) : [];

  const normalizedFiles = files.flatMap((file) => {
    const path = typeof file.path === "string" ? file.path.trim() : "";
    if (!path) return [];
    const filePatch = typeof file.patch === "string" ? file.patch : undefined;
    let filePlus = toFiniteNumber(file.plus ?? file.additions);
    let fileMinus = toFiniteNumber(file.minus ?? file.deletions);
    if (filePlus === 0 && fileMinus === 0 && filePatch) {
      const patchStats = countUnifiedDiffLines(filePatch);
      filePlus = patchStats.plus;
      fileMinus = patchStats.minus;
    }
    return [{
      path,
      oldPath: typeof file.old_path === "string"
        ? file.old_path.trim() || undefined
        : typeof file.oldPath === "string"
          ? file.oldPath.trim() || undefined
          : undefined,
      plus: filePlus,
      minus: fileMinus,
      patch: filePatch,
      status: typeof file.status === "string" ? file.status : undefined,
    }];
  });

  if (normalizedFiles.length) {
    const filePlus = normalizedFiles.reduce((sum, file) => sum + file.plus, 0);
    const fileMinus = normalizedFiles.reduce((sum, file) => sum + file.minus, 0);
    plus = plus || filePlus;
    minus = minus || fileMinus;
  }

  const patches = files
    .map((file) => typeof file.patch === "string" ? file.patch.trim() : "")
    .filter(Boolean);
  const patch = directPatch ?? (patches.length
    ? patches.join("\n\n")
    : typeof raw.raw === "string"
      ? raw.raw
      : undefined);

  if (plus === 0 && minus === 0 && patch) {
    const patchStats = countUnifiedDiffLines(patch);
    plus = patchStats.plus;
    minus = patchStats.minus;
  }

  return plus || minus || patch || normalizedFiles.length
    ? {
        plus,
        minus,
        patch,
        ...(normalizedFiles.length ? { files: normalizedFiles } : {}),
      }
    : undefined;
};

export const reduceToolCallStart = (
  prev: ReadonlyMap<string, ToolCallRecord>,
  e: ToolCallEvent,
  now: number = Date.now(),
): Map<string, ToolCallRecord> => {
  const next = new Map(prev);
  const existing = prev.get(e.id);
  const incomingSeq = typeof e.seq === "number" && Number.isFinite(e.seq) ? e.seq : undefined;
  const existingSeq = typeof existing?.seq === "number" && Number.isFinite(existing.seq) ? existing.seq : undefined;
  if (existing && incomingSeq !== undefined && existingSeq !== undefined && incomingSeq <= existingSeq) {
    return next;
  }
  const terminal = existing && isTerminalToolCallStatus(existing.status);
  next.set(e.id, {
    ...existing,
    id: e.id,
    name: e.name,
    args: e.args ?? {},
    status: terminal
      ? existing.status
      : e.status === "pending"
        ? "pending"
        : "running",
    startedAt: existing?.startedAt ?? e.started_at ?? now,
    displayHint: e.display_hint ?? existing?.displayHint,
    inputSummary: e.input_summary ?? existing?.inputSummary,
    resultKind: e.result_kind ?? existing?.resultKind,
    activityKind: e.activity_kind ?? existing?.activityKind,
    visibility: e.visibility ?? existing?.visibility,
    groupId: e.group_id ?? existing?.groupId,
    stepId: e.step_id ?? existing?.stepId,
    taskId: e.task_id ?? existing?.taskId,
    turnId: e.turn_id ?? existing?.turnId,
    seq: e.seq ?? existing?.seq,
    iterationId: e.iteration_id ?? existing?.iterationId,
    phase: e.phase ?? existing?.phase,
    // Live line counts while arguments stream in; a committed tool_result diff
    // (reduceToolCallResult) still wins over these provisional numbers.
    diff: normalizeToolDiff(e.diff) ?? existing?.diff,
  });
  return next;
};

export const reduceToolCallResult = (
  prev: ReadonlyMap<string, ToolCallRecord>,
  e: ToolResultEvent,
  now: number = Date.now(),
): Map<string, ToolCallRecord> => {
  const existing = prev.get(e.id);
  if (!existing) return new Map(prev);
  const next = new Map(prev);
  const incoming: ToolCallRecord = {
    ...existing,
    status: e.status === "blocked"
      ? "blocked"
      : e.status === "failed"
        ? "failed"
        : e.status === "timeout"
          ? "timeout"
          : e.status === "partial"
            ? "partial"
            : e.status === "cancelled"
              ? "cancelled"
            : e.is_error
              ? "failed"
              : "success",
    summary: e.summary,
    artifactId: e.artifact_id ?? existing.artifactId,
    artifactKind: e.artifact_kind ?? existing.artifactKind,
    artifactMediaType: e.artifact_media_type ?? existing.artifactMediaType,
    artifactBytes: e.artifact_bytes ?? existing.artifactBytes,
    sourceUrl: e.source_url,
    extractionStatus: e.extraction_status,
    contentPreview: e.content_preview,
    evidenceType: e.evidence_type,
    displaySummary: e.display_summary,
    resultKind: e.result_kind ?? existing.resultKind,
    activityKind: e.activity_kind ?? existing.activityKind,
    visibility: e.visibility ?? existing.visibility,
    groupId: e.group_id ?? existing.groupId,
    stepId: e.step_id ?? existing.stepId,
    taskId: e.task_id ?? existing.taskId,
    turnId: e.turn_id ?? existing.turnId,
    seq: e.seq ?? existing.seq,
    limitation: e.limitation,
    provider: e.provider,
    providerErrorType: e.provider_error_type,
    errorInfo: e.error_info,
    errorKind: e.error_kind ?? e.error_info?.error_kind ?? e.error_info?.code,
    userSummary: e.user_summary ?? e.error_info?.user_summary ?? e.error_info?.user_message,
    developerDetail: e.developer_detail ?? e.error_info?.developer_detail,
    recoverable: e.recoverable ?? e.error_info?.recoverable,
    projection: e.projection ?? e.error_info?.projection,
    durationMs: e.duration_ms,
    iterationId: e.iteration_id ?? existing.iterationId,
    phase: e.phase ?? existing.phase,
    diff: normalizeToolDiff(e.diff) ?? existing.diff,
    outputFiles: e.output_files?.map((file) => ({
      path: file.path,
      name: file.name,
      size: file.size,
      mimeType: file.mime_type,
      isImage: file.is_image,
    })) ?? existing.outputFiles,
    finishedAt: now,
  };
  next.set(e.id, mergeToolCallResultRecord(existing, incoming));
  return next;
};

export const aggregateDiffBadge = (records: Iterable<ToolCallRecord>): DiffBadge => {
  let plus = 0;
  let minus = 0;
  for (const r of records) {
    // Diff statistics are backend-owned structured data. Do not infer file
    // changes from a human summary, which may contain unrelated +N/-N text.
    if (!r.diff) continue;
    const diffStats = getToolDiffStats(r.diff);
    plus += diffStats.plus;
    minus += diffStats.minus;
  }
  return { plus, minus };
};
