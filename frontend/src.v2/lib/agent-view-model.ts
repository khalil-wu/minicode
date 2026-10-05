import { effectiveSubagentStatus } from "./collaborationDisplay";
import type { SubagentState } from "../stores/types";

export type AgentDisplayStatus = "attention" | "running" | "waiting" | "completed";
export interface AgentLink {
  id: string;
  title: string;
  identityKey: string;
}

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
  relativeTimeLabel: string;
  identityKey: string;
  teammateName?: string;
  teamName?: string;
  parent?: AgentLink;
  dependencies: AgentLink[];
  blockedDependencies: AgentLink[];
  children: AgentLink[];
  awaitingPlanApproval: boolean;
  effectiveStatus: SubagentState["status"];
  hasResult: boolean;
  needsResult: boolean;
  canStop: boolean;
  executionMode: "blocking" | "background";
  activityLog: string[];
  resultContent?: string;
  resultError?: string;
}

const titleFor = (agent: SubagentState): string => {
  return String(agent.objective || agent.summary || "").trim() || "子任务";
};

const displayStatus = (agent: SubagentState): AgentDisplayStatus => {
  if (agent.cleanupPending) return "attention";
  if (isLiveAgent(agent) && agent.awaitingPlanApproval) return "attention";
  if (isLiveAgent(agent) && agent.needsInput) return "attention";
  if (isLiveAgent(agent) && agent.isIdle) return "waiting";
  switch (effectiveSubagentStatus(agent)) {
    case "error":
      return "attention";
    case "blocked":
      return agent.blockedBy?.length || String(agent.waitingOn || "").trim()
          ? "waiting"
          : "attention";
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
  if (agent.cleanupPending) return "清理未完成";
  if (isLiveAgent(agent) && agent.awaitingPlanApproval) return "等待批准计划";
  if (isLiveAgent(agent) && agent.needsInput) return "等待你回复";
  if (isLiveAgent(agent) && agent.isIdle) return "待命";
  const effective = effectiveSubagentStatus(agent);
  if (effective === "blocked") {
    if (agent.blockedBy?.length || String(agent.waitingOn || "").trim()) return "等待中";
    return "已阻塞";
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
  if (agent.cleanupPending) return agent.cleanupReason || "进程退出尚未确认，请重试停止";
  if (isLiveAgent(agent) && agent.awaitingPlanApproval) return "计划已提交，等待你审阅";
  if (isLiveAgent(agent) && agent.needsInput) {
    return String(agent.waitingOn || agent.detail || agent.summary || "").trim()
      || "需要你的输入才能继续";
  }
  if (isLiveAgent(agent) && agent.isIdle) return "当前工作已结束，可以补充下一步说明";
  const status = effectiveSubagentStatus(agent);
  const activity = String(agent.currentActivity || "").trim();
  const detail = String(agent.detail || "").trim();
  const summary = String(agent.summary || "").trim();
  const error = String(agent.resultError || "").trim();
  if (status === "error") return error || "执行失败";
  if (status === "partial") {
    if (agent.terminationReason === "deadline_exceeded") {
      return summary || "已保留可用结果";
    }
    return summary || "已完成部分工作";
  }
  if (status === "cancelled") return agent.terminationInitiator === "user" ? "已由你停止" : "任务已取消";
  if (status === "blocked" && agent.blockedBy?.length) return "等待前置任务完成";
  if (status === "blocked") {
    return String(agent.waitingOn || agent.currentActivity || agent.detail || agent.summary || "").trim()
      || "任务已阻塞";
  }
  if (status === "pending") return "等待启动";
  if (status === "done") return summary || activity || "任务已完成";
  return activity || detail || summary || "正在执行";
};

export const sanitizeAgentResultContent = (content?: string): string => {
  return String(content || "").trim();
};

export const sanitizeAgentResultError = (content?: string): string => {
  return String(content || "").trim();
};

const projectedResult = (agent: SubagentState, field: "resultContent" | "resultError"): string | undefined => {
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

const isLiveAgent = (agent: SubagentState): boolean => ["pending", "running", "blocked"].includes(agent.status);

const agentLink = (agent: SubagentState): AgentLink => ({
  id: agent.id,
  title: agent.teammateName || titleFor(agent),
  identityKey: agent.agentPath || agent.id,
});

/** Count exact display states; a stopped or failed task is never "running". */
export const agentStatusSummary = (views: AgentView[]): string => {
  const counts = new Map<string, number>();
  for (const view of views) counts.set(view.statusLabel, (counts.get(view.statusLabel) ?? 0) + 1);
  return [...counts].map(([label, count]) => `${count} ${label}`).join(" · ");
};

const relativeTimeLabelFor = (agent: SubagentState, now: number): string => {
  const timestamp = agent.lastEventAt ?? agent.lastProgressAt;
  if (!timestamp || !Number.isFinite(timestamp)) return "";
  const elapsedMs = Math.max(0, now - timestamp);
  if (elapsedMs < 60_000) return "刚刚";
  const minutes = Math.floor(elapsedMs / 60_000);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时`;
  const days = Math.floor(hours / 24);
  return `${days} 天`;
};

export function projectAgentViews(
  agents: SubagentState[],
  now = Date.now(),
): AgentView[] {
  const resolve = (id: string) => agents.find((agent) => agent.id === id || agent.taskId === id);
  const links = (ids: string[] = []) => ids.flatMap((id) => {
    const agent = resolve(id);
    return agent ? [agentLink(agent)] : [];
  });
  return [...agents]
    .filter((agent) => agent.role !== "message" && agent.role !== "workflow")
    // Keep the creation order stable within each status group so progress
    // events update a row in place instead of making the list jump around.
    .sort((left, right) => rank[displayStatus(left)] - rank[displayStatus(right)])
    .map((source) => {
      const effectiveStatus = effectiveSubagentStatus(source);
      const executionMode = source.background ? "background" as const : "blocking" as const;
      const status = displayStatus(source);
      const projectedContent = projectedResult(source, "resultContent");
      const resultError = projectedResult(source, "resultError");
      const resultContent = projectedContent && projectedContent !== resultError
        ? projectedContent
        : undefined;
      const terminalWithoutResult = ["done", "partial", "cancelled", "error"].includes(effectiveStatus);
      const needsResult = Boolean(
        !resultContent
        && !resultError
        && (source.resultAvailable || terminalWithoutResult),
      );
      const parent = source.parentRunId ? resolve(source.parentRunId) : undefined;
      return {
        id: source.id,
        title: titleFor(source),
        summary: summaryFor(source),
        status,
        statusLabel: statusLabelFor(source, status),
        relativeTimeLabel: relativeTimeLabelFor(source, now),
        identityKey: source.agentPath || source.id,
        teammateName: source.teammateName,
        teamName: source.teamName,
        parent: parent ? agentLink(parent) : undefined,
        dependencies: links(source.dependsOn),
        blockedDependencies: links(source.blockedBy),
        children: agents.filter((agent) => agent.parentRunId === source.id).map(agentLink),
        awaitingPlanApproval: isLiveAgent(source) && Boolean(source.awaitingPlanApproval),
        effectiveStatus,
        hasResult: Boolean(resultError || resultContent || source.resultAvailable),
        needsResult,
        canStop: Boolean(source.cleanupPending) || ["pending", "running", "blocked"].includes(effectiveStatus),
        executionMode,
        activityLog: (source.activityLog ?? [])
          .map((entry) => String(entry || "").trim())
          .filter(Boolean),
        resultContent,
        resultError,
      };
    });
}

export function projectAllAgentViews(agents: SubagentState[], now = Date.now()): AgentView[] {
  return projectAgentViews(agents, now);
}

export function visibleAgentChips(agents: SubagentState[], limit = 3): { agents: AgentView[]; hiddenCount: number } {
  const projected = projectAgentViews(agents);
  return { agents: projected.slice(0, limit), hiddenCount: Math.max(0, projected.length - limit) };
}
