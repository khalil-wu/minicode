import {
  coordinatorNoticeKindForSubagent,
  effectiveSubagentStatus,
  userFacingCoordinatorNoticeForSubagent,
  type CoordinatorNoticeKind,
} from "./collaborationDisplay";
import type { SubagentState } from "../stores/types";
import type { ActivityItem } from "../agent-loop/activity-item";
import { projectSubagentActivityItems } from "../agent-loop/projection/project-subagent-activity-items";

export type AgentDisplayStatus = "attention" | "running" | "waiting" | "completed";
export type { CoordinatorNoticeKind } from "./collaborationDisplay";

export interface AgentView {
  id: string;
  title: string;
  summary: string;
  status: AgentDisplayStatus;
  statusLabel: string;
  elapsedLabel: string;
  roleLabel: string;
  effectiveStatus: SubagentState["status"];
  coordinatorNoticeKind: CoordinatorNoticeKind | null;
  isWorkflow: boolean;
  activity: string;
  detail: string;
  progressTrace: string;
  stats: string;
  metadataChips: string[];
  runtimeRows: AgentRuntimeRow[];
  activityItems: ActivityItem[];
  milestones: AgentMilestone[];
  hasResult: boolean;
  needsResult: boolean;
  hasKnownProgress: boolean;
  progressPercent: number;
  canStop: boolean;
  workflowId?: string;
  workflowName?: string;
  workflowMode?: string;
  nodeId?: string;
  taskId?: string;
  objective?: string;
  blockedBy?: string[];
  dependsOn?: string[];
  order?: number;
  lastProgressAt?: number;
  lastEventAt?: number;
  resultAvailable?: boolean;
  resultContent?: string;
  resultError?: string;
  source: SubagentState;
}

export interface AgentRuntimeRow {
  label: string;
  value: string;
  tone?: "warning" | "muted";
}

export interface AgentMilestone {
  label: string;
  done: boolean;
}

const isWorkflow = (agent: SubagentState): boolean =>
  agent.role === "workflow" || agent.id.startsWith("workflow-");

const firstLine = (value?: string): string => String(value || "").trim().split(/\r?\n/).find(Boolean)?.trim() || "";

const compactList = (items?: string[], limit = 2): string => {
  if (!items?.length) return "";
  const visible = items.slice(0, limit).join(", ");
  return items.length > limit ? `${visible} +${items.length - limit}` : visible;
};

const blocksFinalReply = (agent: SubagentState): boolean => {
  if (typeof agent.blocksFinalReply === "boolean") return agent.blocksFinalReply;
  if (typeof agent.requiredForFinal === "boolean") return agent.requiredForFinal;
  return Boolean(agent.workflowId && agent.role !== "workflow");
};

const isLowValueDetail = (value: string): boolean => {
  const normalized = value.trim().toLowerCase();
  return !normalized
    || /^(?:running task|task running|working|processing|in progress)[.!…]*$/.test(normalized)
    || /^iteration \d+(?:\/\d+)?/.test(normalized)
    || /\bcall_[a-z0-9_-]{8,}\b/i.test(normalized)
    || /^(?:tool started|running)\s*:?\s*[a-z0-9_.-]+$/i.test(normalized)
    || /^\d+(?:\.\d+)?s elapsed$/i.test(normalized);
};

const userVisibleLine = (value?: string): string => {
  const line = firstLine(value);
  return line && !isLowValueDetail(line) ? line : "";
};

const roleLabel = (role?: string): string => {
  switch ((role || "").toLowerCase()) {
    case "workflow": return "任务组";
    case "explore":
    case "explorer": return "探索";
    case "review":
    case "reviewer": return "审查";
    case "verification":
    case "verify":
    case "verifier": return "验证";
    case "implement":
    case "worker": return "执行";
    case "general-purpose": return "通用";
    case "subagent": return "执行";
    default: return role || "任务";
  }
};

const progressTrace = (agent: SubagentState): string => {
  switch ((agent.progressSource || "").toLowerCase()) {
    case "tool_call": return "工具调用";
    case "agent.progress": return "进度事件";
    default: return agent.progressSource || "";
  }
};

const metadataChips = (agent: SubagentState): string[] => [
  agent.nodeId ? `任务 ${agent.nodeId}` : "",
  blocksFinalReply(agent) ? "阻塞最终答复" : agent.requiredForFinal === false ? "不阻塞最终答复" : "",
  agent.readOnly ? "只读" : "",
  agent.blockedBy?.length ? `等待 ${compactList(agent.blockedBy)}` : "",
  agent.dependsOn?.length && !agent.blockedBy?.length ? `依赖 ${compactList(agent.dependsOn)}` : "",
  agent.writeScope?.length ? `写入 ${compactList(agent.writeScope)}` : "",
].filter(Boolean);

const cleanTitle = (value: string): string => value
  .replace(/^(?:ready|blocked|pending|task updated|task output|task created):\s*/i, "")
  .replace(/^\[[^\]]+\]\s*/, "")
  .trim();

const titleFor = (agent: SubagentState): string => {
  const coordinatorNotice = userFacingCoordinatorNoticeForSubagent(agent);
  if (coordinatorNotice) return userVisibleLine(agent.objective) || coordinatorNotice;
  const title = cleanTitle(userVisibleLine(agent.objective) || userVisibleLine(agent.summary) || agent.nodeId || agent.role || agent.id);
  const workflowName = String(agent.workflowName || "").trim();
  if (workflowName && title.toLowerCase().startsWith(`${workflowName.toLowerCase()}:`)) {
    return title.slice(workflowName.length + 1).trim();
  }
  return title;
};

const elapsedLabel = (agent: SubagentState, now: number): string => {
  if (typeof agent.durationMs === "number") {
    const seconds = Math.max(0, Math.round(agent.durationMs / 1000));
    return seconds < 60 ? `${seconds}s` : `${Math.round(seconds / 60)}m`;
  }
  const timestamp = agent.lastProgressAt || agent.lastEventAt;
  if (!timestamp) return effectiveSubagentStatus(agent) === "pending" ? "等待" : "";
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.round(seconds / 60)}m`;
};

const statsLabel = (agent: SubagentState, now: number): string => {
  const duration = typeof agent.durationMs === "number"
    ? `${Math.max(0, agent.durationMs / 1000).toFixed(1)}s`
    : "";
  const timestamp = agent.lastEventAt;
  const age = timestamp
    ? (() => {
        const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
        if (seconds < 60) return `${seconds}s 前`;
        const minutes = Math.round(seconds / 60);
        return minutes < 60 ? `${minutes}m 前` : `${Math.round(minutes / 60)}h 前`;
      })()
    : "";
  return [duration, age].filter(Boolean).join(" · ");
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
      case "duplicate_delegation": return "已收敛";
      case "capacity": return "待收敛";
      default: return "等待依赖";
    }
  }
  if (effective === "running") return "运行中";
  if (effective === "pending") return "等待中";
  if (effective === "done") return "已完成";
  if (effective === "partial") {
    return agent.terminationReason === "deadline_exceeded" ? "达到时限" : "部分完成";
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
  if (status === "partial") return userVisibleLine(agent.summary) || "已保留部分结果";
  if (status === "cancelled") return agent.terminationInitiator === "user" ? "已由你停止" : "任务已取消";
  if (status === "blocked" && agent.blockedBy?.length) return `等待前置任务：${compactList(agent.blockedBy, 3)}`;
  if (status === "pending") return "等待启动";
  if (status === "done") return userVisibleLine(agent.summary) || activity || "任务已完成";
  return activity || detail || userVisibleLine(agent.summary) || "正在执行";
};

const activityFor = (agent: SubagentState): string => {
  if (userVisibleLine(agent.currentActivity)) return userVisibleLine(agent.currentActivity);
  if (userVisibleLine(agent.detail)) return userVisibleLine(agent.detail);
  return summaryFor(agent);
};

const detailFor = (agent: SubagentState): string =>
  agent.detail && agent.detail !== agent.currentActivity && userVisibleLine(agent.detail)
    ? userVisibleLine(agent.detail)
    : "";

const runtimeRows = (agent: SubagentState, status: SubagentState["status"], now: number): AgentRuntimeRow[] => {
  const waitingOn = userVisibleLine(agent.waitingOn);
  const currentTool = userVisibleLine(agent.currentTool);
  const waiting = waitingOn
    || (agent.blockedBy?.length ? compactList(agent.blockedBy, 3) : "")
    || (status === "pending" ? "启动" : status === "running" ? currentTool || "执行" : "");
  const activity = userVisibleLine(agent.currentActivity);
  const updated = agent.lastProgressAt || agent.lastEventAt;
  const age = updated ? elapsedLabel({ ...agent, durationMs: undefined, lastProgressAt: updated }, now) : "";
  return [
    activity ? { label: "活动", value: activity } : null,
    waiting ? {
      label: "等待",
      value: waiting,
      tone: status === "blocked" || status === "pending" ? "warning" as const : "muted" as const,
    } : null,
    age ? { label: "最近更新", value: `${age} 前`, tone: "muted" as const } : null,
    agent.workflowId && agent.role !== "workflow"
      ? {
          label: "最终答复",
          value: blocksFinalReply(agent) ? "阻塞" : "不阻塞",
          tone: blocksFinalReply(agent) ? "warning" as const : "muted" as const,
        }
      : null,
  ].filter((row): row is AgentRuntimeRow => Boolean(row));
};

const progressPercent = (agent: SubagentState): number => {
  if (typeof agent.iteration !== "number" || !agent.maxIterations || agent.maxIterations <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((agent.iteration / agent.maxIterations) * 100)));
};

const rank: Record<AgentDisplayStatus, number> = {
  attention: 0,
  running: 1,
  waiting: 2,
  completed: 3,
};

export function projectAgentViews(agents: SubagentState[], now = Date.now()): AgentView[] {
  return agents
    .filter((agent) => agent.role !== "message" && !isWorkflow(agent))
    .map((source) => {
      const status = displayStatus(source);
      const effectiveStatus = effectiveSubagentStatus(source);
      const activity = activityFor(source);
      const terminalWithoutResult = ["done", "partial", "cancelled", "error"].includes(effectiveStatus);
      const needsResult = Boolean(
        !source.resultContent
        && !source.resultError
        && (source.resultAvailable || terminalWithoutResult),
      );
      const activityItems = projectSubagentActivityItems(source);
      const coordinatorNoticeKind = coordinatorNoticeKindForSubagent(source) as CoordinatorNoticeKind;
      return {
        id: source.id,
        title: titleFor(source),
        summary: summaryFor(source),
        status,
        statusLabel: statusLabelFor(source, status),
        elapsedLabel: elapsedLabel(source, now),
        roleLabel: roleLabel(source.role),
        effectiveStatus,
        coordinatorNoticeKind,
        isWorkflow: isWorkflow(source),
        activity,
        detail: detailFor(source),
        progressTrace: progressTrace(source),
        stats: statsLabel(source, now),
        metadataChips: metadataChips(source),
        runtimeRows: runtimeRows(source, effectiveStatus, now),
        activityItems,
        milestones: activityItems.map((item) => ({
          label: item.title,
          done: item.status === "completed" || item.status === "failed" || item.status === "cancelled",
        })),
        hasResult: Boolean(source.resultError || source.resultContent || source.resultAvailable || effectiveStatus === "done" || effectiveStatus === "error"),
        needsResult,
        hasKnownProgress: typeof source.iteration === "number" && Boolean(source.maxIterations && source.maxIterations > 0),
        progressPercent: progressPercent(source),
        canStop: effectiveStatus === "running" && source.id.startsWith("subagent-"),
        workflowId: source.workflowId,
        workflowName: source.workflowName,
        workflowMode: source.workflowMode,
        nodeId: source.nodeId,
        taskId: source.taskId,
        objective: source.objective,
        blockedBy: source.blockedBy,
        dependsOn: source.dependsOn,
        order: source.order,
        lastProgressAt: source.lastProgressAt,
        lastEventAt: source.lastEventAt,
        resultAvailable: source.resultAvailable,
        resultContent: source.resultContent,
        resultError: source.resultError,
        source,
      };
    })
    .sort((left, right) =>
      rank[left.status] - rank[right.status]
      || (right.source.lastProgressAt || right.source.lastEventAt || 0) - (left.source.lastProgressAt || left.source.lastEventAt || 0),
    );
}

export function projectAllAgentViews(agents: SubagentState[], now = Date.now()): AgentView[] {
  return agents
    .filter((agent) => agent.role !== "message")
    .map((source) => {
      const status = displayStatus(source);
      const effectiveStatus = effectiveSubagentStatus(source);
      const activity = activityFor(source);
      const needsResult = Boolean(source.resultAvailable && !source.resultContent && !source.resultError);
      const activityItems = projectSubagentActivityItems(source);
      const coordinatorNoticeKind = coordinatorNoticeKindForSubagent(source) as CoordinatorNoticeKind;
      return {
        id: source.id,
        title: titleFor(source),
        summary: summaryFor(source),
        status,
        statusLabel: statusLabelFor(source, status),
        elapsedLabel: elapsedLabel(source, now),
        roleLabel: roleLabel(source.role),
        effectiveStatus,
        coordinatorNoticeKind,
        isWorkflow: isWorkflow(source),
        activity,
        detail: detailFor(source),
        progressTrace: progressTrace(source),
        stats: statsLabel(source, now),
        metadataChips: metadataChips(source),
        runtimeRows: runtimeRows(source, effectiveStatus, now),
        activityItems,
        milestones: activityItems.map((item) => ({
          label: item.title,
          done: item.status === "completed" || item.status === "failed" || item.status === "cancelled",
        })),
        hasResult: Boolean(source.resultError || source.resultContent || source.resultAvailable || effectiveStatus === "done" || effectiveStatus === "error"),
        needsResult,
        hasKnownProgress: typeof source.iteration === "number" && Boolean(source.maxIterations && source.maxIterations > 0),
        progressPercent: progressPercent(source),
        canStop: effectiveStatus === "running" && source.id.startsWith("subagent-"),
        workflowId: source.workflowId,
        workflowName: source.workflowName,
        workflowMode: source.workflowMode,
        nodeId: source.nodeId,
        taskId: source.taskId,
        objective: source.objective,
        blockedBy: source.blockedBy,
        dependsOn: source.dependsOn,
        order: source.order,
        lastProgressAt: source.lastProgressAt,
        lastEventAt: source.lastEventAt,
        resultAvailable: source.resultAvailable,
        resultContent: source.resultContent,
        resultError: source.resultError,
        source,
      };
    })
    .sort((left, right) =>
      rank[left.status] - rank[right.status]
      || (right.source.lastProgressAt || right.source.lastEventAt || 0) - (left.source.lastProgressAt || left.source.lastEventAt || 0),
    );
}

export function visibleAgentChips(agents: SubagentState[], limit = 3): { agents: AgentView[]; hiddenCount: number } {
  const projected = projectAgentViews(agents);
  return { agents: projected.slice(0, limit), hiddenCount: Math.max(0, projected.length - limit) };
}
