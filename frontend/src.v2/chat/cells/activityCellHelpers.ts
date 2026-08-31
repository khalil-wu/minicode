import type { ActivityCellState } from "./cellTypes";
import { purifyToolErrorText } from "../errorMessages";
import { readableToolLabel } from "../toolDisplayName";

export interface ActivityDetail {
  label: string;
  target: string;
  targetKind: "file" | "url" | "text";
  lineInfo?: string;
  count: number;
  durationMs: number | null;
}

export type ActivityToolRecord = NonNullable<ActivityCellState["toolCallRecords"]>[number];

/** Web fetch records share the web-search activity envelope but render as a
 * separate, flat transcript action. Keep the discriminator in one place so
 * labels, targets, icons, and surface styling cannot drift apart. */
export function isWebFetchRecord(record: ActivityToolRecord): boolean {
  const name = String(record.name || "").trim().toLowerCase();
  const resultKind = String(record.resultKind || "").trim().toLowerCase();
  return name === "web_fetch" || name === "webfetch" || resultKind === "web";
}

export function isWebFetchActivity(
  cell: Pick<ActivityCellState, "activityKind" | "toolCallRecords">,
): boolean {
  const records = cell.toolCallRecords ?? [];
  return cell.activityKind === "webSearch"
    && records.length > 0
    && records.every(isWebFetchRecord);
}

export type PlanUpdateStep = {
  step: string;
  status: "pending" | "in_progress" | "completed";
};

/** Read the canonical update_plan payload used by the live composer plan. */
export function planUpdateSteps(record: ActivityToolRecord): PlanUpdateStep[] {
  if (record.name !== "update_plan" || !record.args || typeof record.args !== "object") return [];
  const rawPlan = (record.args as Record<string, unknown>).plan;
  if (!Array.isArray(rawPlan)) return [];
  return rawPlan.flatMap((rawStep): PlanUpdateStep[] => {
    if (!rawStep || typeof rawStep !== "object") return [];
    const step = String((rawStep as Record<string, unknown>).step ?? "").trim();
    const status = String((rawStep as Record<string, unknown>).status ?? "pending");
    if (!step || !["pending", "in_progress", "completed"].includes(status)) return [];
    return [{ step, status: status as PlanUpdateStep["status"] }];
  });
}

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
  const title = readableToolLabel(cell.title);
  const records = cell.toolCallRecords ?? [];
  if (cell.activityKind === "webSearch" && records.length > 0) {
    const isFetch = records.every(isWebFetchRecord);
    const names = records.map((record) => String(record.name || "").toLowerCase());
    const isSearch = names.every((name) => /web_search|websearch/.test(name));
    const hasFailure = records.some((record) => ["failed", "blocked", "timeout", "cancelled"].includes(String(record.status)));
    if (!hasFailure && (isFetch || isSearch)) {
      const action = isFetch ? "获取网页" : "搜索网页";
      const running = records.some((record) => ["running", "pending"].includes(String(record.status)));
      return action;
    }
  }
  return title;
}

export function readableRecordLabel(record: ActivityToolRecord): string {
  const summary = readableToolLabel(record.displaySummary);
  const operation = readableToolLabel(record.displayHint || record.name);
  const normalized = summary.match(/^(?:Completed|Failed|Blocked|Cancelled|Timed out):\s*(.+)$/i)?.[1]?.trim();
  return normalized && operation && normalized.toLowerCase() === operation.toLowerCase()
    ? operation
    : summary || operation || "工具";
}

/** Return the user-facing target already present in the tool call arguments.
 * Codex/pi render the call's input beside its operation name; use the typed
 * args as the fallback when a provider did not populate inputSummary. */
export function recordInputTarget(record: ActivityToolRecord): string {
  const args = record.args && typeof record.args === "object"
    ? record.args as Record<string, unknown>
    : {};
  const firstString = (candidates: unknown[]): string => candidates.find((candidate): candidate is string =>
    typeof candidate === "string" && candidate.trim().length > 0,
  )?.trim() || "";
  const name = String(record.name || "").trim().toLowerCase();
  const activityKind = String(record.activityKind || "").trim().toLowerCase();
  const path = firstString([
    args.file_path,
    args.filePath,
    args.path,
    args.target,
    args.filename,
    args.directory,
  ]);
  const query = firstString([
    args.query,
    args.pattern,
  ]);
  const url = firstString([args.url, record.sourceUrl]);

  if (name === "list_files") return path || record.inputSummary?.trim() || ".";

  // Search operations are most useful when the searched expression is shown
  // first. Keep the location beside it when the tool supplied one, so a row
  // can be understood without expanding its details (for example:
  // `AgentTimeline · frontend/src.v2`).
  if (activityKind === "workspacesearch" || ["grep_files", "glob_files", "search_files"].includes(name)) {
    return [query, path].filter(Boolean).join(" · ") || firstString([
      record.inputSummary,
      record.displaySummary,
    ]);
  }
  if (activityKind === "websearch") {
    const isFetch = isWebFetchRecord(record);
    return (isFetch ? url : [query, url].filter(Boolean).join(" · ")) || firstString([
      record.inputSummary,
      record.displaySummary,
    ]);
  }

  if (
    activityKind === "browser"
    || name === "browser_control"
    || name === "browser"
    || name === "computer"
  ) {
    return firstString([
      url,
      args.target_id,
      args.selector,
      args.action,
      record.inputSummary,
    ]);
  }

  return firstString([
    path,
    args.command,
    args.selector,
    args.artifact_id,
    args.artifactId,
    record.inputSummary,
    record.sourceUrl,
  ]);
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
  if (isFileTarget && (
    ["file", "edit"].includes(String(record.resultKind || "").toLowerCase())
    || String(record.activityKind || "").toLowerCase() === "fileread"
    || record.name === "read_file"
    || record.name === "read_artifact"
  )) return "file";
  return "text";
};

/** Build the unmerged detail for one authoritative tool record. */
export function describeRecordDetail(
  record: ActivityToolRecord,
  developerMode: boolean,
): ActivityDetail | null {
  if (record.name === "update_plan") return null;
  const label = developerMode
    ? readableToolLabel(record.displayHint || record.name)
    : readableRecordLabel(record);
  const target = recordInputTarget(record);
  if (!developerMode && !target && !record.displaySummary && !record.displayHint) return null;
  const targetKind = detailTargetKind(record, target);
  const lineInfo = readFileLineInfoLabel(record);
  return {
    label,
    target,
    targetKind,
    lineInfo: lineInfo || undefined,
    count: 1,
    durationMs: record.durationMs ?? null,
  };
}

export function describeRecordDetails(
  records: NonNullable<ActivityCellState["toolCallRecords"]>,
  developerMode: boolean,
): ActivityDetail[] {
  const details = new Map<string, ActivityDetail>();
  for (const record of records) {
    // update_plan has a dedicated structured disclosure in ActivityCell. A
    // generic "Update plan" row would duplicate the activity title and hide
    // the useful step/status payload behind a meaningless tool name.
    const detail = describeRecordDetail(record, developerMode);
    if (!detail) continue;
    const key = `${detail.label}\n${detail.targetKind}\n${detail.target}\n${detail.lineInfo || ""}`;
    const existing = details.get(key);
    if (existing) {
      existing.count += 1;
      existing.durationMs = (existing.durationMs ?? 0) + (record.durationMs ?? 0);
      continue;
    }
    details.set(key, detail);
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
    return count ? `${count} 项` : "";
  }
  if (record.name === "read_file") {
    const declared = output.match(/\((\d+) lines\b/i)?.[1];
    if (declared) return `${declared} 行`;
    const body = output.split(/\r?\n\r?\n\[(?:content_hash|range_hash):/i)[0];
    const count = body ? body.split(/\r?\n/).length : 0;
    return count > 0 ? `${count} 行` : "";
  }
  if (record.name === "glob_files") {
    const count = output.match(/Found (\d+) matching files/i)?.[1];
    if (count) return `${count} 个文件`;
    if (/No files matched/i.test(output)) return "0 个文件";
  }
  if (record.name === "grep_files") {
    const declared = output.match(/(?:找到|Found)\s*(\d+)\s*(?:条结果|matches?)/i)?.[1];
    if (declared) return `${declared} 条结果`;
    if (/^\(no matches\)$/i.test(output.trim())) return "0 条结果";
    const count = resultLines(output).length;
    if (count > 0) {
      const mode = String(record.args?.output_mode || "files_with_matches");
      return `${count} ${mode === "files_with_matches" ? "个文件" : "条结果"}`;
    }
  }
  return "";
}

export function hasOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): boolean {
  return Boolean(records?.some((record) => recordOutputText(record)));
}

/** Return the bounded output belonging to exactly one tool record. */
export function getRecordOutputPreview(record: ActivityToolRecord): string {
  const output = recordOutputText(record);
  if (!output) return "";
  const isReadResult = record.name === "read_file"
    || String(record.activityKind || "").toLowerCase() === "fileread"
    || String(record.resultKind || "").toLowerCase() === "file";
  if (isReadResult) {
    // A read result is already bounded by the requested line range. Preserve
    // the complete bounded body so each file remains paired with its record.
    return output;
  }
  const tail = output.split("\n").slice(-24).join("\n");
  return tail.length > 1600 ? `...${tail.slice(-1600)}` : tail;
}

export function getOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): string {
  const outputs = (records ?? [])
    .map(getRecordOutputPreview)
    .filter(Boolean);
  const combined = outputs.join("\n\n");
  return combined.length > 1600 ? `...${combined.slice(-1600)}` : combined;
}

export function isLongRunning(startedAt: number | undefined): boolean {
  return startedAt != null && Date.now() - startedAt > 10_000;
}
