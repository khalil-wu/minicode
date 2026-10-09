import type { ActivityCellState, HistoryCellState } from "./cellTypes";
import { purifyToolErrorText } from "../errorMessages";
import { readableToolLabel } from "../toolDisplayName";
import { safeJsonParse } from "../../lib/safe-parse";
import { isCommandToolRecord, isToolCallExecuting } from "../../lib/tool-call-reducer";

export interface ActivityDetail {
  label: string;
  target: string;
  targetKind: "file" | "url" | "text";
  lineInfo?: string;
  count: number;
  durationMs: number | null;
}

export type ActivityToolRecord = NonNullable<ActivityCellState["toolCallRecords"]>[number];

export function isBrowserRecord(record: ActivityToolRecord): boolean {
  return ["browser_control", "browser", "computer"].includes(record.name)
    || record.resultKind === "browser"
    || (record.activityKind === "browser" && record.name !== "preview_server");
}

const BROWSER_ACTION_LABELS: Record<string, string> = {
  navigate: "Navigate",
  get_dom: "Read DOM",
  screenshot: "Capture screenshot",
  discover: "Discover browser",
  list_targets: "List browser targets",
  get_url: "Read URL",
  get_text: "Read text",
  get_html: "Read HTML",
  wait_for_element: "Wait for element",
  get_console_logs: "Read console logs",
  get_network_logs: "Read network logs",
  click: "Click",
  type: "Type",
  press_key: "Press key",
  scroll: "Scroll",
  evaluate: "Evaluate JavaScript",
};

/** File evidence used to resolve transcript links, shared with invalidation. */
export function knownFilePathsForCell(cell: HistoryCellState): string[] {
  if (cell.kind === "diff") return cell.files.filter((file) => file.changeType !== "deleted").map((file) => file.path);
  if (cell.kind !== "activity") return [];
  return (cell.toolCallRecords ?? []).filter((record) => record.status === "success").flatMap((record) => [
    ...(typeof record.args.file_path === "string" ? [record.args.file_path] : []),
    ...(record.diff?.files ?? []).filter((file) => file.status !== "deleted").map((file) => file.path),
    ...(record.outputFiles ?? []).map((file) => file.path),
  ]);
}

/** Web fetch records share the web-search activity envelope but render as a
 * separate, flat transcript action. Keep the discriminator in one place so
 * labels, targets, icons, and surface styling cannot drift apart. */
export function isWebFetchRecord(record: ActivityToolRecord): boolean {
  const name = String(record.name || "").trim().toLowerCase();
  const resultKind = String(record.resultKind || "").trim().toLowerCase();
  return name === "web_fetch" || name === "webfetch" || resultKind === "web";
}

export function recordPresentationStatus(record: ActivityToolRecord): ActivityToolRecord["status"] {
  // Older fetch receipts called the transport successful even when extraction
  // failed. Use the actual extraction outcome in the existing action row,
  // while keeping the original receipt and diagnostics unchanged.
  if (isWebFetchRecord(record) && record.status === "success") {
    if (record.extractionStatus === "failed") return "failed";
    if (record.extractionStatus === "partial") return "partial";
  }
  return record.status;
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
  const records = cell.toolCallRecords ?? [];
  if (records.length > 0 && records.every(isBrowserRecord)) {
    return [...new Set(records.map(readableRecordLabel))].join(" · ");
  }
  const running = records.length > 0 ? records.some(isToolCallExecuting) : cell.status === "running";
  // The projection owns classification; persisted render summaries must not
  // replace the operation with a success receipt or a shortened target.
  if (cell.activityKind === "fileRead") return running ? "正在读取" : "读取文件";
  if (cell.activityKind === "workspaceList") return running ? "正在查看目录" : "查看目录";
  if (cell.activityKind === "workspaceSearch") return running ? "正在搜索" : "搜索";
  if (cell.activityKind === "fileChange") return running ? "正在编辑" : "编辑文件";
  if (cell.activityKind === "commandExecution") return running ? "正在运行" : "运行命令";
  if (cell.activityKind === "planning") return "更新计划";
  if (cell.activityKind === "skill") return `${running ? "正在读取技能" : "读取技能"}${cell.skill?.name ? ` ${cell.skill.name}` : ""}`;
  if (cell.activityKind === "webSearch") return isWebFetchActivity(cell) ? running ? "正在读取网页" : "读取网页" : running ? "正在搜索网页" : "搜索网页";
  if (records.length > 0) return [...new Set(records.map(readableRecordLabel))].join(" · ");
  return readableToolLabel(cell.title, running);
}

export function readableRecordLabel(record: ActivityToolRecord): string {
  if (record.name === "task") return "启动子智能体";
  if (record.name === "send_message") return "发送消息";
  if (record.name === "task_stop") return "停止子智能体";
  if (record.name === "update_plan") return "更新计划";
  if (isBrowserRecord(record)) {
    const action = stringArg(record.args.action).toLowerCase();
    return BROWSER_ACTION_LABELS[action] || action || "Browser";
  }
  if (isCodeModeRecord(record)) return "运行";
  const running = isToolCallExecuting(record);
  if (record.name.startsWith("mcp__") && record.displayHint) return record.displayHint;
  const operation = readableToolLabel(record.name, running);
  // Canonical built-ins win over old localized displayHint/displaySummary.
  // Unknown tools retain their supplied action label, not invocation IDs.
  return operation !== record.name
    ? operation
    : readableToolLabel(record.displayHint || record.displaySummary || record.name, running);
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
  ) || "";
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

  // Code cells compose operations; their scripts, polling ids and generated
  // input summaries are runtime instructions, not a user's work target.
  if (isCodeModeRecord(record)) return "";
  if (name === "task") {
    const tasks = Array.isArray(args.parallel_tasks) ? args.parallel_tasks : [args];
    return tasks.flatMap((task) => task && typeof task === "object"
      ? [firstString([task.name, task.description])] : []).filter(Boolean).join("、");
  }
  if (["monitor", "task_status", "task_create", "task_get", "task_list", "task_update", "task_output"].includes(name)) return firstString([args.title, args.description]);
  if (name === "read_artifact") return firstString([args.name, args.path, record.sourceUrl]);

  if (isBrowserRecord(record)) {
    const input = firstString([record.inputSummary]);
    return firstString([
      url,
      args.selector,
      args.key,
      ["Browser", record.name, args.action, args.target_id].includes(input.trim()) ? "" : input,
    ]);
  }

  if (name === "list_files") return path || firstString([record.inputSummary]) || ".";
  if (activityKind === "commandexecution" || ["run_command", "shell_command", "exec_command", "bash"].includes(name)) {
    return firstString([args.command, args.cmd, record.inputSummary]);
  }
  if (name === "apply_patch") {
    return path || (record.diff?.files ?? []).map((file) => file.path).join(", ") || firstString([record.inputSummary]);
  }

  // Search operations are most useful when the searched expression is shown
  // first. Keep the location beside it when the tool supplied one, so a row
  // can be understood without expanding its details (for example:
  // `AgentTimeline · frontend/src.v2`).
  if (activityKind === "workspacesearch" || ["grep_files", "glob_files", "search_files"].includes(name)) {
    return [query, path].filter(Boolean).join(" · ") || firstString([
      record.inputSummary,
    ]);
  }
  if (activityKind === "websearch" || ["web_fetch", "webfetch", "web_search", "websearch"].includes(name)) {
    const isFetch = isWebFetchRecord(record);
    return (isFetch ? url : [query, url].filter(Boolean).join(" · ")) || firstString([
      record.inputSummary,
    ]);
  }

  return firstString([
    path,
    args.command,
    args.cmd,
    args.selector,
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
  const label = readableRecordLabel(record);
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

export const isCodeModeRecord = (record: ActivityToolRecord): boolean =>
  record.name === "tool_exec" || record.name === "tool_wait";

export const taskResultAgentIds = (record: ActivityToolRecord): string[] => {
  const value = `${record.outputPreview || ""}\n${record.summary || ""}`;
  return [...new Set([...value.matchAll(/\bsubagent-[a-z0-9]+\b/gi)].map((match) => match[0]))];
};

export const codeModeOwnError = (record: ActivityToolRecord): string => {
  const raw = record.outputPreview || record.summary || record.stdoutPreview || record.contentPreview || "";
  const report = safeJsonParse<Record<string, unknown> | null>(raw, null);
  if (report && typeof report.cell_id === "string" && typeof report.status === "string") {
    return typeof report.error === "string" ? report.error.trim() : "";
  }
  return ["failed", "blocked", "timeout", "cancelled", "partial"].includes(record.status) ? raw.trim() : "";
};

/** Present results, never the execute/wait protocol envelope. The original
 * record remains untouched for routing, recovery, Inspector and export.
 * Only these two known tools produce this envelope: code, DOM and stdout
 * that happen to mention cell/call ids must retain their actual bytes. */
export function getRecordOutputText(record: ActivityToolRecord): string {
  // A yielded native command keeps its original tool name. The backend's
  // typed command presentation, like a monitor receipt, owns its user output.
  if (record.contentPreview && (isCommandToolRecord(record)
    || ["monitor", "task", "task_status", "task_get", "task_list", "task_create", "task_update", "task_output"].includes(record.name))) {
    return record.contentPreview;
  }
  const raw = [record.outputPreview, record.summary, record.stdoutPreview, record.contentPreview]
    .find((value) => value?.trim()) || "";
  if (isCodeModeRecord(record)) {
    const report = safeJsonParse<Record<string, unknown> | null>(raw, null);
    if (report && typeof report.cell_id === "string" && typeof report.status === "string") {
      const output = Array.isArray(report.output) ? report.output.filter((value): value is string => typeof value === "string") : [];
      const completed = Array.isArray(report.completed_tools) ? report.completed_tools as Array<{ tool: string; status: string; output?: string }> : [];
      return [
        typeof report.error === "string" ? purifyToolErrorText(report.error) : "",
        ...output,
        typeof report.output_preview === "string" ? report.output_preview : "",
        ...completed.map((tool) => [readableToolLabel(tool.tool), tool.status, tool.output].filter(Boolean).join("\n")),
        typeof report.discarded_unawaited_tool_calls === "number" && report.discarded_unawaited_tool_calls > 0
          ? `${report.discarded_unawaited_tool_calls} unawaited tool calls were discarded; their completion is not confirmed.` : "",
      ].filter(Boolean).join("\n");
    }
  }
  const output = purifyToolErrorText(raw);
  if (record.name === "monitor") {
    const bodyStart = raw.indexOf('<untrusted_tool_result source="monitor">');
    const noOutputStart = raw.indexOf("<no output captured yet>");
    const splitAt = bodyStart >= 0 ? bodyStart : noOutputStart;
    if (splitAt >= 0) {
      const header = raw.slice(0, splitAt);
      const processStatus = header.match(/Background command \S+ \(([^)]+)\)/)?.[1];
      const evidence = header.split(/\r?\n/).filter((line) => /^(?:command|cwd|exit_code):|^\[showing |^Process cleanup is still pending/.test(line));
      return [processStatus ? `Process: ${processStatus}` : "", ...evidence, purifyToolErrorText(raw.slice(splitAt))].filter(Boolean).join("\n");
    }
    if (output.startsWith("Background commands:\n")) return output.split(/\r?\n/).map((line) => {
      const command = line.match(/^- \S+: (\S+) exit=(\S+) output=.+? cwd=(.*?) command=(.*?) started_at=/);
      return command ? `- ${command[1]} · ${command[4]}\ncwd: ${command[3]}\nexit: ${command[2]}` : line;
    }).join("\n");
    if (/^Wrote \d+ UTF-8 bytes to background command \S+/.test(output)) return output.replace(/(to background command) \S+?(?= and closed stdin\.|\.$)/, "$1");
    if (/^Background command '[^']+' was not found\.$/.test(output)) return "Requested background command was not found.";
  }
  if (record.name === "task_status") {
    // Legacy receipts have a protocol header and an optional Result body.
    // Only rewrite the header; delegated content is never regex-redacted.
    const sectionIds = Array.isArray(record.args.subagent_ids) ? record.args.subagent_ids as string[] : [];
    const sections = sectionIds.reduce((parts, id) => parts.flatMap((part) => part.split(`### ${id}\n`)), [raw]).filter(Boolean);
    return sections.map((section) => {
      const resultStart = section.indexOf("\nResult:\n");
      const header = resultStart >= 0 ? section.slice(0, resultStart) : section;
      let index = 0;
      const presentedHeader = header.split(/\r?\n/).flatMap((line) => {
        if (/^Background task:/.test(line)) return [];
        if (/^Subagent \S+ status:/.test(line)) return [line.replace(/^Subagent \S+ status:/, "Agent status:")];
        if (/^No subagent found for \S+\.$/.test(line)) return ["Requested agent was not found."];
        if (/^- \S+ \[/.test(line)) return [line.replace(/^- \S+ (\[)/, `- Agent ${++index} $1`)];
        return [line];
      }).join("\n");
      if (resultStart < 0) return presentedHeader;
      const statsStart = section.lastIndexOf("\nStats:");
      const result = section.slice(resultStart + "\nResult:\n".length, statsStart > resultStart ? statsStart : undefined)
        .replace(/\nFull result artifact: [^\n]+\.?$/, "\n完整结果已保存。");
      return `${presentedHeader}\nResult:\n${result}`;
    }).join("\n\n");
  }
  if (["task_create", "task_update", "task_output"].includes(record.name) && record.status === "success" && record.displaySummary) return record.displaySummary;
  if (record.name === "task_get") {
    const outputStart = output.indexOf("\nOutputs:\n");
    const descriptionStart = output.indexOf("\nDescription:");
    const headerEnd = descriptionStart >= 0 ? descriptionStart : outputStart >= 0 ? outputStart : output.length;
    const header = output.slice(0, headerEnd).split(/\r?\n/).flatMap((line, index) => {
      if (index === 0) return [line.replace(/^\S+ (\[)/, "$1")];
      if (line.startsWith("Assignee:")) return ["Assignee: delegated agent"];
      if (/^(?:Blocks|Blocked by):/.test(line)) return [line.replace(/: .+$/, ": dependent tasks")];
      return [line];
    }).join("\n");
    const remainder = output.slice(headerEnd);
    // The legacy text protocol does not retain structured output authors.
    // Remove only its first output-author slot, never list syntax within the
    // delegated output. New receipts use the typed presentation above.
    return header + (outputStart >= 0 ? remainder.replace(/(\nOutputs:\n)- [^:\n]+: /, "$1") : remainder);
  }
  if (record.name === "task_list") return output.split(/\r?\n/).map((line) => line
    .replace(/^(\d+\. )\S+ (\[[^\]]*\])/, "$1$2")
    .replace(/ -> \S+ (?=\(\d+ output\(s\)\)$)/, " ")
    .replace(/^   deps: blocks=(\S+) blocked_by=(\S+)$/, (_match, blocks: string, blocked: string) => `   Dependencies: ${blocks === "-" ? "no downstream tasks" : `${blocks.split(",").length} downstream tasks`}, ${blocked === "-" ? "no prerequisites" : `${blocked.split(",").length} prerequisites`}`)).join("\n");
  if (record.name === "preview_server" && raw.trim().startsWith("{")) {
    const preview = safeJsonParse<Record<string, unknown> | null>(raw, null);
    if (preview && typeof preview.url === "string" && typeof preview.status === "string") {
      const verification = preview.verification as { ok?: boolean; status_code?: number; error?: string } | undefined;
      return [`Preview: ${preview.status}`, preview.url,
        verification ? `HTTP verification: ${verification.ok ? "passed" : "failed"}${verification.status_code ? ` · ${verification.status_code}` : ""}${verification.error ? `\n${verification.error}` : ""}` : ""].filter(Boolean).join("\n");
    }
  }
  if (isBrowserRecord(record) && record.status === "success") {
    const action = stringArg(record.args.action).toLowerCase();
    if (["navigate", "screenshot", "get_url"].includes(action)) {
      return output.split(/\r?\n/).flatMap((line) => {
        if (/^(?:loaderId|Artifact|Base64 chars):/.test(line)) return [];
        if (line.startsWith("Target:")) {
          const title = line.match(/^Target:\s*\S+\s+(.+)$/)?.[1];
          return title ? [`Title: ${title}`] : [];
        }
        return [line];
      }).join("\n");
    }
    if (["discover", "list_targets"].includes(action)) {
      return output.split(/\r?\n/)
        .filter((line) => !/^(?:CDP endpoint|Protocol-Version):/.test(line))
        .map((line) => line.replace(/^(\d+\. )\S+ (\[[^\]]*\])/, "$1$2"))
        .join("\n");
    }
  }
  return record.name === "read_file" ? stripModelOnlyReadMetadata(output) : output;
}

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
  const output = getRecordOutputText(record);
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
  return Boolean(records?.some((record) => getRecordOutputText(record)));
}

/** Return the bounded output belonging to exactly one tool record. */
export function getRecordOutputPreview(record: ActivityToolRecord): string {
  const output = getRecordOutputText(record);
  if (!output) return "";
  const isReadResult = record.name === "read_file"
    || String(record.activityKind || "").toLowerCase() === "fileread"
    || String(record.resultKind || "").toLowerCase() === "file";
  if (isReadResult) {
    // Line numbers and edit hashes belong to the model's read protocol. The
    // disclosure shows code; the header already carries the requested range.
    return record.name === "read_file"
      ? output.replace(/^[ \t]*\d+→/gm, "")
      : output;
  }
  // The backend has already bounded this evidence. An explicit disclosure
  // must not silently discard its first lines or a useful failure context.
  return output;
}

export function getOutputPreview(records?: NonNullable<ActivityCellState["toolCallRecords"]>): string {
  const outputs = (records ?? [])
    .map(getRecordOutputPreview)
    .filter(Boolean);
  return outputs.join("\n\n");
}

export function isLongRunning(startedAt: number | undefined): boolean {
  return startedAt != null && Date.now() - startedAt > 10_000;
}
