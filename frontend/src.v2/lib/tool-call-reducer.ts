import type { ToolCallEvent, ToolResultEvent } from "../protocol/events";

export type ToolCallStatus = "pending" | "running" | "success" | "failed" | "blocked";

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
  limitation?: string;
  durationMs?: number;
  displayHint?: string;
  inputSummary?: string;
  startedAt: number;
  finishedAt?: number;
  diff?: { plus: number; minus: number; patch?: string };
}

const toFiniteNumber = (value: unknown): number => {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
};

export const normalizeToolDiff = (value: unknown): ToolCallRecord["diff"] | undefined => {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;

  const directPlus = toFiniteNumber(raw.plus ?? raw.additions);
  const directMinus = toFiniteNumber(raw.minus ?? raw.deletions);
  if (directPlus || directMinus || typeof raw.patch === "string") {
    return {
      plus: directPlus,
      minus: directMinus,
      patch: typeof raw.patch === "string" ? raw.patch : undefined,
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
    status: e.status === "blocked" ? "blocked" : e.status === "failed" ? "failed" : e.is_error ? "failed" : "success",
    summary: e.summary,
    artifactId: e.artifact_id,
    sourceUrl: e.source_url,
    extractionStatus: e.extraction_status,
    contentPreview: e.content_preview,
    evidenceType: e.evidence_type,
    displaySummary: e.display_summary,
    resultKind: e.result_kind,
    limitation: e.limitation,
    durationMs: e.duration_ms,
    diff: normalizeToolDiff(e.diff) ?? existing.diff,
    finishedAt: now,
  });
  return next;
};

export interface DiffBadge {
  plus: number;
  minus: number;
}

const DIFF_RE = /([+-])(\d+)/g;

export const aggregateDiffBadge = (records: Iterable<ToolCallRecord>): DiffBadge => {
  let plus = 0;
  let minus = 0;
  for (const r of records) {
    if (r.diff) {
      plus += r.diff.plus;
      minus += r.diff.minus;
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
