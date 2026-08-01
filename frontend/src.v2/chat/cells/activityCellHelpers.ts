import type { ActivityCellState } from "./cellTypes";
import { purifyToolErrorText } from "../errorMessages";

export interface ActivityDetail {
  label: string;
  target: string;
  targetKind: "file" | "url" | "text";
  lineInfo?: string;
  count: number;
  durationMs: number | null;
}

type ActivityToolRecord = NonNullable<ActivityCellState["toolCallRecords"]>[number];

export function shortTarget(value: string): string {
  const text = String(value).replace(/\\/g, "/").trim();
  if (!text) return "";
  const fileName = text.split("/").pop() ?? text;
  return fileName.length > 50 ? `${fileName.slice(0, 47)}...` : fileName;
}

export function shortCommand(command: string): string {
  const text = command.trim();
  return text.length > 60 ? `${text.slice(0, 57)}...` : text;
}

export function readableFallback(value: string | undefined): string {
  return String(value || "").trim();
}

export function readableTimelineTitle(cell: ActivityCellState): string {
  return readableFallback(cell.title);
}

export function readableRecordLabel(record: ActivityToolRecord): string {
  const summary = readableFallback(record.displaySummary);
  const operation = readableFallback(record.displayHint || record.name);
  const normalized = summary.match(/^(?:Completed|Failed|Blocked|Cancelled|Timed out):\s*(.+)$/i)?.[1]?.trim();
  return normalized && operation && normalized.toLowerCase() === operation.toLowerCase()
    ? operation
    : summary || operation || "Tool";
}

/** Return the user-facing target already present in the tool call arguments.
 * Codex/pi render the call's input beside its operation name; use the typed
 * args as the fallback when a provider did not populate inputSummary. */
export function recordInputTarget(record: ActivityToolRecord): string {
  const args = record.args && typeof record.args === "object"
    ? record.args as Record<string, unknown>
    : {};
  const candidates = [
    args.file_path,
    args.filePath,
    args.directory,
    args.path,
    args.target,
    args.filename,
    args.query,
    args.pattern,
    args.url,
    args.command,
    args.selector,
    record.inputSummary,
    record.sourceUrl,
  ];
  const value = candidates.find((candidate): candidate is string =>
    typeof candidate === "string" && candidate.trim().length > 0,
  );
  return value?.trim() || "";
}

export function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

export function firstHttpUrl(value?: string): string {
  return value?.match(/https?:\/\/[^\s)]+/i)?.[0] ?? "";
}

export function stringArg(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function fileLabel(path: string): string {
  return path.replace(/\\/g, "/").split("/").pop() || path;
}

const positiveInteger = (value: unknown): number | null => {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : null;
};

export function readFileLineInfoLabel(record: ActivityToolRecord): string {
  const start = positiveInteger(record.args?.start_line ?? record.args?.startLine);
  const end = positiveInteger(record.args?.end_line ?? record.args?.endLine);
  if (start && end) return `L${start}-L${end}`;
  if (start) return `L${start}+`;
  if (end) return `L1-L${end}`;
  return "";
}

const detailTargetKind = (record: ActivityToolRecord, target: string): ActivityDetail["targetKind"] => {
  if (isHttpUrl(target)) return "url";
  const args = record.args && typeof record.args === "object"
    ? record.args as Record<string, unknown>
    : {};
  const isFileTarget = ["read_file", "write_file", "edit_file", "apply_patch"].includes(record.name)
    || typeof args.file_path === "string"
    || typeof args.filename === "string";
  if (isFileTarget && ["file", "edit"].includes(String(record.resultKind || "").toLowerCase())) return "file";
  return "text";
};

export function describeRecordDetails(
  records: NonNullable<ActivityCellState["toolCallRecords"]>,
  developerMode: boolean,
): ActivityDetail[] {
  const details = new Map<string, ActivityDetail>();
  for (const record of records) {
    const label = developerMode ? record.name : readableRecordLabel(record);
    const target = recordInputTarget(record);
    if (!developerMode && !target && !record.displaySummary && !record.displayHint) continue;
    const targetKind = detailTargetKind(record, target);
    const lineInfo = readFileLineInfoLabel(record);
    const key = `${label}\n${targetKind}\n${target}\n${lineInfo}`;
    const existing = details.get(key);
    if (existing) {
      existing.count += 1;
      existing.durationMs = (existing.durationMs ?? 0) + (record.durationMs ?? 0);
      continue;
    }
    details.set(key, {
      label,
      target,
      targetKind,
      lineInfo: lineInfo || undefined,
      count: 1,
      durationMs: record.durationMs ?? null,
    });
  }
  return [...details.values()];
}

const recordOutputText = (record: ActivityToolRecord): string =>
  stripModelOnlyReadMetadata(purifyToolErrorText(
    record.outputPreview?.trim()
    || record.contentPreview?.trim()
    || record.stdoutPreview?.trim()
    || record.summary?.trim()
    || "",
  ));

const stripModelOnlyReadMetadata = (value: string): string => value
  .split(/\r?\n/)
  .filter((line) => !/^\[(?:content_hash|range_hash|range only)[^\]]*\]$/i.test(line.trim()))
  .join("\n")
  .trimEnd();

const resultLines = (value: string): string[] => value
  .split(/\r?\n/)
  .map((line) => line.trim())
  .filter((line) => line && !/^\[(?:content_hash|range_hash|results? (?:truncated|incomplete))/i.test(line));

/** Build compact result metadata from the canonical typed tool result. */
export function recordOutcomeMeta(record: ActivityToolRecord): string {
  if (record.status !== "success") return "";
  const output = recordOutputText(record);
  if (!output) return "";

  if (record.name === "list_files") {
    const count = output.match(/\((\d+) entries\)/i)?.[1];
    return count ? `${count} entries` : "";
  }
  if (record.name === "read_file") {
    const declared = output.match(/\((\d+) lines\b/i)?.[1];
    if (declared) return `${declared} lines`;
    const body = output.split(/\r?\n\r?\n\[(?:content_hash|range_hash):/i)[0];
    const count = body ? body.split(/\r?\n/).length : 0;
    return count > 0 ? `${count} lines` : "";
  }
  if (record.name === "glob_files") {
    const count = output.match(/Found (\d+) matching files/i)?.[1];
    if (count) return `${count} files`;
    if (/No files matched/i.test(output)) return "0 files";
  }
  if (record.name === "grep_files") {
    const declared = output.match(/(?:找到|Found)\s*(\d+)\s*(?:条结果|matches?)/i)?.[1];
    if (declared) return `${declared} matches`;
    if (/^\(no matches\)$/i.test(output.trim())) return "0 matches";
    const count = resultLines(output).length;
    if (count > 0) {
      const mode = String(record.args?.output_mode || "files_with_matches");
      return `${count} ${mode === "files_with_matches" ? "files" : "results"}`;
    }
  }
  return "";
}

export function hasOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): boolean {
  return Boolean(records?.some((record) => recordOutputText(record)));
}

export function getOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): string {
  const record = records?.length ? records[records.length - 1] : undefined;
  const output = record ? recordOutputText(record) : "";
  if (record?.name === "read_file") {
    // A read result is already bounded by the tool's requested line range.
    // Preserve it from the first requested line so the visible body agrees
    // with the Lx-Ly label; tailing is only appropriate for command logs.
    return output;
  }
  const tail = output.split("\n").slice(-24).join("\n");
  return tail.length > 1600 ? `...${tail.slice(-1600)}` : tail;
}

export function isLongRunning(startedAt: number | undefined): boolean {
  return startedAt != null && Date.now() - startedAt > 10_000;
}

export function getLongRunningExplanation(): string {
  return "This operation is still running.";
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${seconds % 60}s`;
}
