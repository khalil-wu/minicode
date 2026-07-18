import type { ToolCallEvent, ToolErrorInfo, ToolResultEvent } from "../protocol/events";

export type ToolCallStatus = "pending" | "running" | "success" | "failed" | "blocked" | "partial" | "timeout";

export interface ToolCallRecord {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: ToolCallStatus;
  summary?: string;
  artifactId?: string;
  sourceUrl?: string;
  extractionStatus?: string;
  contentPreview?: string;
  evidenceType?: string;
  displaySummary?: string;
  resultKind?: string;
  activityKind?: string;
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
  iterationId?: string;
  phase?: string;
  displayScope?: string;
  panelHint?: string;
  requiresAttention?: boolean;
  startedAt: number;
  finishedAt?: number;
  outputPreview?: string;
  stdoutPreview?: string;
  stderrPreview?: string;
  diff?: { plus: number; minus: number; patch?: string };
}

export const isAgentControlToolName = (name: string): boolean =>
  /^(?:task(?:_.*)?|workflow(?:_.*)?|team_.*|send_message|message_list)$/i.test(name.trim());

const normalizedProjectionValue = (value: string | undefined): string => String(value || "").trim().toLowerCase();

export const isCommandToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.activityKind) === "commandexecution"
  || normalizedProjectionValue(record.resultKind) === "command";

export const isFileChangeToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.activityKind) === "filechange"
  || normalizedProjectionValue(record.resultKind) === "edit"
  || Boolean(record.diff);

export const isWorkspaceSearchToolRecord = (record: ToolCallRecord): boolean =>
  normalizedProjectionValue(record.activityKind) === "workspacesearch";

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
  const panelHint = normalizedProjectionValue(record.panelHint);
  return resultKind === "browser" || resultKind === "preview" || panelHint === "browser" || panelHint === "preview";
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
  if (directPlus || directMinus || directPatch) {
    const patchStats = directPatch ? countUnifiedDiffLines(directPatch) : { plus: 0, minus: 0 };
    const shouldUsePatchStats = directPlus === 0 && directMinus === 0;
    return {
      plus: shouldUsePatchStats ? patchStats.plus : directPlus,
      minus: shouldUsePatchStats ? patchStats.minus : directMinus,
      patch: directPatch,
    };
  }

  const stats = raw.stats && typeof raw.stats === "object" ? raw.stats as Record<string, unknown> : {};
  let plus = toFiniteNumber(stats.additions);
  let minus = toFiniteNumber(stats.deletions);
  const files = Array.isArray(raw.files) ? raw.files.filter((item): item is Record<string, unknown> =>
    Boolean(item && typeof item === "object")
  ) : [];

  if (files.length) {
    const filePlus = files.reduce((sum, file) => sum + toFiniteNumber(file.additions), 0);
    const fileMinus = files.reduce((sum, file) => sum + toFiniteNumber(file.deletions), 0);
    plus = plus || filePlus;
    minus = minus || fileMinus;
  }

  const patches = files
    .map((file) => typeof file.patch === "string" ? file.patch.trim() : "")
    .filter(Boolean);
  const patch = patches.length
    ? patches.join("\n\n")
    : typeof raw.raw === "string"
      ? raw.raw
      : undefined;

  if (plus === 0 && minus === 0 && patch) {
    const patchStats = countUnifiedDiffLines(patch);
    plus = patchStats.plus;
    minus = patchStats.minus;
  }

  return plus || minus || patch ? { plus, minus, patch } : undefined;
};

export const reduceToolCallStart = (
  prev: ReadonlyMap<string, ToolCallRecord>,
  e: ToolCallEvent,
  now: number = Date.now(),
): Map<string, ToolCallRecord> => {
  const next = new Map(prev);
  next.set(e.id, {
    id: e.id,
    name: e.name,
    args: e.args ?? {},
    status: e.status === "pending" ? "pending" : "running",
    startedAt: e.started_at ?? now,
    displayHint: e.display_hint,
    inputSummary: e.input_summary,
    resultKind: e.result_kind,
    activityKind: e.activity_kind,
    groupId: e.group_id,
    stepId: e.step_id,
    taskId: e.task_id,
    turnId: e.turn_id,
    seq: e.seq,
    iterationId: e.iteration_id,
    phase: e.phase,
    displayScope: e.display_scope,
    panelHint: e.panel_hint,
    requiresAttention: e.requires_attention,
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
  next.set(e.id, {
    ...existing,
    status: e.status === "blocked"
      ? "blocked"
      : e.status === "failed"
        ? "failed"
        : e.status === "timeout"
          ? "timeout"
          : e.status === "partial"
            ? "partial"
            : e.is_error
              ? "failed"
              : "success",
    summary: e.summary,
    artifactId: e.artifact_id,
    sourceUrl: e.source_url,
    extractionStatus: e.extraction_status,
    contentPreview: e.content_preview,
    evidenceType: e.evidence_type,
    displaySummary: e.display_summary,
    resultKind: e.result_kind,
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
    displayScope: e.display_scope ?? existing.displayScope,
    panelHint: e.panel_hint ?? existing.panelHint,
    requiresAttention: e.requires_attention ?? existing.requiresAttention,
    diff: normalizeToolDiff(e.diff) ?? existing.diff,
    finishedAt: now,
  });
  return next;
};

const DIFF_RE = /([+-])(\d+)/g;

export const aggregateDiffBadge = (records: Iterable<ToolCallRecord>): DiffBadge => {
  let plus = 0;
  let minus = 0;
  for (const r of records) {
    if (r.diff) {
      const diffStats = getToolDiffStats(r.diff);
      plus += diffStats.plus;
      minus += diffStats.minus;
      continue;
    }
    if (!r.summary) continue;
    let m: RegExpExecArray | null;
    DIFF_RE.lastIndex = 0;
    while ((m = DIFF_RE.exec(r.summary)) !== null) {
      const n = parseInt(m[2], 10);
      if (m[1] === "+") plus += n;
      else minus += n;
    }
  }
  return { plus, minus };
};
