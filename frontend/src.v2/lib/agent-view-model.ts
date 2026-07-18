import {
  coordinatorNoticeKindForSubagent,
  effectiveSubagentStatus,
  userFacingCoordinatorNoticeForSubagent,
  type CoordinatorNoticeKind,
} from "./collaborationDisplay";
import type { ChatMessage, SubagentState } from "../stores/types";

export type AgentDisplayStatus = "attention" | "running" | "waiting" | "completed";
export type { CoordinatorNoticeKind } from "./collaborationDisplay";

/**
 * Ordinary UI projection for delegated work.
 *
 * Runtime diagnostics (call ids, elapsed time, tool/iteration counts, progress
 * traces and protocol milestones) deliberately stay on SubagentState and in
 * Inspector. Components consuming AgentView should only be able to render
 * information a person needs to understand or act on the delegated task.
 */
export interface AgentView {
  id: string;
  title: string;
  summary: string;
  status: AgentDisplayStatus;
  statusLabel: string;
  effectiveStatus: SubagentState["status"];
  isWorkflow: boolean;
  hasResult: boolean;
  needsResult: boolean;
  canStop: boolean;
  canResume: boolean;
  handledByParent: boolean;
  executionMode: "blocking" | "background";
  activityLog: string[];
  resultContent?: string;
  resultError?: string;
}

export const hasCompletedAssistantReply = (messages: ChatMessage[]): boolean => {
  const latest = [...messages].reverse().find(
    (message) => message.role === "user" || message.role === "assistant",
  );
  if (!latest || latest.role !== "assistant" || latest.isStreaming) return false;
  if (latest.terminalStatus === "failed" || latest.terminalStatus === "interrupted") return false;
  const hasExplicitFinalBlock = latest.blocks?.some((block) => (
    block.type === "text"
    && Boolean(block.content.trim())
    && (
      block.visibility === "final"
      || block.phase === "final"
      || block.sealed === true
      || block.source === "model_final"
    )
  ));
  if (hasExplicitFinalBlock) return true;
  return Boolean(!latest.blocks?.length && latest.content.trim());
};

const isWorkflow = (agent: SubagentState): boolean =>
  agent.role === "workflow" || agent.id.startsWith("workflow-");

const plainTextLine = (value: string): string => value
  .replace(/^#{1,6}\s+/, "")
  .replace(/^>\s*/, "")
  .replace(/^[-*+]\s+/, "")
  .replace(/^\d+[.)]\s+/, "")
  .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
  .replace(/(?:\*\*|__|`)/g, "")
  .trim();

const INTERNAL_REPORT_HEADING_RE =
  /^#{1,6}\s*(?:result|evidence(?:\s+claims)?|changes|verification|risks?\s+or\s+blockers?)\s*$/i;

const stripInternalReminderBlocks = (value?: string): string => String(value || "")
  .replace(/<system-reminder\b[^>]*>[\s\S]*?<\/system-reminder>/gi, "")
  .replace(/<system-reminder\b[^>]*>[\s\S]*$/gi, "")
  .trim();

const isLowValueDetail = (value: string): boolean => {
  const normalized = value.trim().toLowerCase();
  return !normalized
    || INTERNAL_REPORT_HEADING_RE.test(normalized)
    || /^(?:running task|task running|working|processing|in progress)[.!…]*$/.test(normalized)
    || /^iteration \d+(?:\/\d+)?/.test(normalized)
    || /\bcall_[a-z0-9_-]{8,}\b/i.test(normalized)
    || /\bart_[a-z0-9_-]{6,}\b/i.test(normalized)
    || /\bmcp__[a-z0-9_.-]+/i.test(normalized)
    || /^(?:tool started|running|using tool)\s*:?\s*[a-z0-9_.:/-]+$/i.test(normalized)
    || /^(?:read|grep|glob|search|write|edit|patch|run|execute)_[a-z0-9_.-]+$/i.test(normalized)
    || /\b(?:workflow|checkpoint|tool[_ -]?call|node)[_ -]?id\s*[:=]/i.test(normalized)
    || /^\d+(?:\.\d+)?s elapsed$/i.test(normalized)
    || /^timed out[.!…]*$/i.test(normalized)
    || /^(?:ready\s*\/\s*launched|waiting on dependencies)[.!…]*$/i.test(normalized)
    || /^workflow mode\s*:/i.test(normalized)
    || /^task output\s*:/i.test(normalized)
    || /^subagent\s+subagent-[\w-]+.*completed/i.test(normalized)
    || /^stats:\s*\d+\s+iteration/i.test(normalized)
    || /^tools used \(\d+ total\):/i.test(normalized)
    || /^(?:read artifact|read file|grep files|glob files)\b/i.test(normalized)
    || /^agent\s*\d+$/i.test(normalized)
    || /^(?:subagent|task|node)[-_ ][a-z0-9_-]+$/i.test(normalized);
};

const userVisibleLine = (value?: string): string => {
  for (const rawLine of stripInternalReminderBlocks(value).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || /^#{1,6}\s+/.test(line) || isLowValueDetail(line)) continue;
    const plain = plainTextLine(line);
    if (plain && !isLowValueDetail(plain)) return plain;
  }
  return "";
};

const cleanTitle = (value: string): string => value
  .replace(/^(?:ready|blocked|pending|task updated|task output|task created):\s*/i, "")
  .replace(/^\[[^\]]+\]\s*/, "")
  .trim();

const fallbackTitleForRole = (role?: string): string => {
  switch ((role || "").toLowerCase()) {
    case "explore":
    case "explorer":
      return "调研任务";
    case "review":
    case "reviewer":
      return "审查任务";
    case "verification":
    case "verify":
    case "verifier":
      return "验证任务";
    case "implement":
    case "worker":
      return "执行任务";
    default:
      return "子任务";
  }
};

const titleFor = (agent: SubagentState): string => {
  const coordinatorNotice = userFacingCoordinatorNoticeForSubagent(agent);
  if (coordinatorNotice) return userVisibleLine(agent.objective) || coordinatorNotice;
  const title = cleanTitle(userVisibleLine(agent.objective) || userVisibleLine(agent.summary));
  const workflowName = String(agent.workflowName || "").trim();
  if (workflowName && title.toLowerCase().startsWith(`${workflowName.toLowerCase()}:`)) {
    return title.slice(workflowName.length + 1).trim() || fallbackTitleForRole(agent.role);
  }
  return title || fallbackTitleForRole(agent.role);
};

const displayStatus = (agent: SubagentState): AgentDisplayStatus => {
  switch (effectiveSubagentStatus(agent)) {
    case "error":
      return "attention";
    case "blocked":
      return coordinatorNoticeKindForSubagent(agent) === "duplicate_delegation" ? "completed" : "attention";
    case "running":
      return "running";
    case "pending":
      return "waiting";
    case "partial":
    case "cancelled":
      return "attention";
    default:
      return "completed";
  }
};

const statusLabel = (status: AgentDisplayStatus): string => {
  switch (status) {
    case "attention": return "需要处理";
    case "running": return "运行中";
    case "waiting": return "等待中";
    case "completed": return "已完成";
  }
};

const statusLabelFor = (agent: SubagentState, status: AgentDisplayStatus): string => {
  const effective = effectiveSubagentStatus(agent);
  if (effective === "blocked") {
    switch (coordinatorNoticeKindForSubagent(agent)) {
      case "collect_results": return "整理中";
      case "duplicate_delegation": return "已跳过";
      case "capacity": return "排队中";
      default: return "等待中";
    }
  }
  if (effective === "running") return "运行中";
  if (effective === "pending") return "等待中";
  if (effective === "done") return "已完成";
  if (effective === "partial") {
    return agent.terminationReason === "deadline_exceeded" ? "已保留结果" : "部分完成";
  }
  if (effective === "cancelled") {
    return agent.terminationInitiator === "user" ? "已停止" : "已取消";
  }
  if (effective === "error") return "失败";
  return statusLabel(status);
};

const summaryFor = (agent: SubagentState): string => {
  const coordinatorNotice = userFacingCoordinatorNoticeForSubagent(agent);
  if (coordinatorNotice) return coordinatorNotice;
  const status = effectiveSubagentStatus(agent);
  const activity = userVisibleLine(agent.currentActivity);
  const detail = userVisibleLine(agent.detail);
  if (status === "error") return userVisibleLine(agent.resultError) || "执行失败";
  if (status === "partial") {
    if (agent.terminationReason === "deadline_exceeded") {
      return userVisibleLine(agent.summary) || "已保留可用结果";
    }
    return userVisibleLine(agent.summary) || "已完成部分工作";
  }
  if (status === "cancelled") return agent.terminationInitiator === "user" ? "已由你停止" : "任务已取消";
  if (status === "blocked" && agent.blockedBy?.length) return "等待前置任务完成";
  if (status === "pending") return "等待启动";
  if (status === "done") return userVisibleLine(agent.summary) || activity || "任务已完成";
  return activity || detail || userVisibleLine(agent.summary) || "正在执行";
};

const isLowValueResultLine = (line: string): boolean => {
  if (!line) return false;
  return (
    INTERNAL_REPORT_HEADING_RE.test(line)
    || /^[-*]\s*(?:none|n\/a)\.?$/i.test(line)
    || /\bcall_[a-z0-9_-]{8,}\b/i.test(line)
    || /\bart_[a-z0-9_-]{6,}\b/i.test(line)
    || /\bmcp__[a-z0-9_.-]+/i.test(line)
    || /^\d+(?:\.\d+)?s elapsed$/i.test(line)
    || /^timed out[.!…]*$/i.test(line)
    || /^subagent\s+subagent-[\w-]+.*completed/i.test(line)
    || /\bsubagent-[a-z0-9_-]{4,}\b/i.test(line)
    || /^stats:\s*\d+\s+iteration/i.test(line)
    || /^tools used \(\d+ total\):/i.test(line)
    || /^recovery summary based on completed tool results:?$/i.test(line)
    || /^internal artifact was read/i.test(line)
    || /^(?:ready\s*\/\s*launched|waiting on dependencies)[.!…]*$/i.test(line)
    || /^workflow mode\s*:/i.test(line)
    || /^task output\s*:/i.test(line)
    || /^(?:read artifact|read file|grep files|glob files)\b/i.test(line)
    || /^\d+\.\s*(?:read file|grep files|glob files|read artifact)\b/i.test(line)
    || /^[-*]\s*(?:read_file|grep_files|glob_files|read_artifact)\(/i.test(line)
    || /\b\d+\s+iteration\(s\)|\b\d+\s+tool call\(s\)/i.test(line)
    || /\b(?:workflow|checkpoint|tool[_ -]?call|node)[_ -]?id\s*[:=]/i.test(line)
    || /<task-notification>|<\/task-notification>|<task-id>|<\/task-id>/i.test(line)
  );
};

/**
 * Remove protocol/runtime noise before result text reaches ordinary UI.
 * Inspector and replay still receive the original SubagentState payload.
 */
export const sanitizeAgentResultContent = (content?: string): string => {
  const text = stripInternalReminderBlocks(content);
  if (!text) return "";
  const lines = text.split(/\r?\n/);
  const kept = lines.filter((line) => !isLowValueResultLine(line.trim()));
  const cleaned = kept.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  return cleaned || "";
};

// Errors are actionable content: only drop pure noise lines (call ids,
// elapsed/iteration counters), never a line carrying real error text.
const isPureNoiseErrorLine = (line: string): boolean => {
  if (!line) return false;
  return (
    /^call_[a-z0-9_-]{8,}$/i.test(line)
    || /^\d+(?:\.\d+)?s elapsed$/i.test(line)
    || /^stats:\s*\d+\s+iteration/i.test(line)
    || /^tools used \(\d+ total\):$/i.test(line)
    || /^iteration \d+(?:\/\d+)?$/i.test(line)
  );
};

/**
 * Conservative sanitizer for error text: strips runtime counters but keeps
 * every line with substantive content (even if it mentions a subagent id),
 * and falls back to the original text rather than an empty string.
 */
export const sanitizeAgentResultError = (content?: string): string => {
  const text = stripInternalReminderBlocks(content);
  if (!text) return "";
  const kept = text
    .split(/\r?\n/)
    .filter((line) => !isPureNoiseErrorLine(line.trim()));
  return kept.join("\n").replace(/\n{3,}/g, "\n\n").trim() || text;
};

const projectedResult = (agent: SubagentState, field: "resultContent" | "resultError"): string | undefined => {
  const coordinatorNotice = userFacingCoordinatorNoticeForSubagent(agent);
  if (coordinatorNotice && agent[field]) return coordinatorNotice;
  const value = field === "resultError"
    ? sanitizeAgentResultError(agent[field])
    : sanitizeAgentResultContent(agent[field]);
  return value || undefined;
};

const rank: Record<AgentDisplayStatus, number> = {
  attention: 0,
  running: 1,
  waiting: 2,
  completed: 3,
};

export function projectAgentViews(
  agents: SubagentState[],
  _now = Date.now(),
  options: { includeWorkflows?: boolean; parentCompleted?: boolean } = {},
): AgentView[] {
  return agents
    .filter((agent) => agent.role !== "message" && (options.includeWorkflows || !isWorkflow(agent)))
    // Keep the creation order stable within each status group so progress
    // events update a row in place instead of making the list jump around.
    .sort((left, right) => rank[displayStatus(left)] - rank[displayStatus(right)])
    .map((source) => {
      const effectiveStatus = effectiveSubagentStatus(source);
      const handledByParent = Boolean(
        options.parentCompleted
        && ["partial", "cancelled", "error"].includes(effectiveStatus),
      );
      const status = handledByParent ? "completed" : displayStatus(source);
      const projectedContent = handledByParent ? undefined : projectedResult(source, "resultContent");
      const resultError = handledByParent ? undefined : projectedResult(source, "resultError");
      const resultContent = projectedContent && projectedContent !== resultError
        ? projectedContent
        : undefined;
      const terminalWithoutResult = ["done", "partial", "cancelled", "error"].includes(effectiveStatus);
      const needsResult = Boolean(
        !handledByParent
        &&
        !resultContent
        && !resultError
        && !source.resultContent
        && !source.resultError
        && (source.resultAvailable || terminalWithoutResult),
      );
      return {
        id: source.id,
        title: titleFor(source),
        summary: handledByParent ? "" : summaryFor(source),
        status,
        statusLabel: handledByParent ? "已由主任务接管" : statusLabelFor(source, status),
        effectiveStatus,
        isWorkflow: isWorkflow(source),
        hasResult: Boolean(!handledByParent && (resultError || resultContent || source.resultAvailable)),
        needsResult,
        canStop: effectiveStatus === "running" && source.id.startsWith("subagent-"),
        canResume: !handledByParent
          && ["partial", "cancelled", "error"].includes(effectiveStatus)
          && source.id.startsWith("subagent-"),
        handledByParent,
        executionMode: source.blocksFinalReply === false || source.requiredForFinal === false
          ? "background"
          : "blocking",
        activityLog: handledByParent
          ? []
          : (source.activityLog ?? []).map(userVisibleLine).filter(Boolean),
        resultContent,
        resultError,
      };
    });
}

export function projectAllAgentViews(agents: SubagentState[], now = Date.now()): AgentView[] {
  return projectAgentViews(agents, now, { includeWorkflows: true });
}

export function visibleAgentChips(agents: SubagentState[], limit = 3): { agents: AgentView[]; hiddenCount: number } {
  const projected = projectAgentViews(agents);
  return { agents: projected.slice(0, limit), hiddenCount: Math.max(0, projected.length - limit) };
}
