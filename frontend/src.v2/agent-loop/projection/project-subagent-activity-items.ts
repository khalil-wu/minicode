import type { SubagentState } from "../../stores/types";
import type { ActivityItem, ActivityItemStatus } from "../activity-item";

const firstLine = (value?: string): string =>
  String(value || "").trim().split(/\r?\n/).find(Boolean)?.trim() || "";

const userVisibleLine = (value?: string): string => {
  const line = firstLine(value);
  if (
    !line
    || /\bcall_[a-z0-9_-]{8,}\b/i.test(line)
    || /\bart_[a-z0-9_-]{6,}\b/i.test(line)
    || /^(?:tool started|running)\s*:?\s*[a-z0-9_.-]+$/i.test(line)
    || /^\d+(?:\.\d+)?s elapsed$/i.test(line)
    || /^(?:ready\s*\/\s*launched|waiting on dependencies)[.!…]*$/i.test(line)
    || /^(?:workflow mode|task output)\s*:/i.test(line)
  ) {
    return "";
  }
  return line;
};

const executionStatus = (status: SubagentState["status"]): ActivityItemStatus => {
  switch (status) {
    case "pending": return "queued";
    case "running": return "running";
    case "blocked": return "blocked";
    case "done": return "completed";
    case "partial": return "completed";
    case "cancelled": return "cancelled";
    case "error": return "failed";
  }
};

const phaseFor = (role?: string): ActivityItem["phase"] => {
  switch ((role || "").toLowerCase()) {
    case "explore":
    case "explorer":
      return "researching";
    case "review":
    case "reviewer":
    case "verification":
    case "verify":
    case "verifier":
      return "verifying";
    case "implement":
    case "worker":
      return "implementing";
    default:
      return undefined;
  }
};

const executionTitle = (agent: SubagentState): string =>
  agent.status === "partial"
    ? agent.terminationReason === "deadline_exceeded" ? "达到时限，已保留部分结果" : "部分完成"
    : agent.status === "cancelled"
      ? agent.terminationInitiator === "user" ? "已停止" : "已取消"
      : agent.status === "error"
        ? "执行失败"
        : userVisibleLine(agent.currentActivity)
          || userVisibleLine(agent.detail)
          || userVisibleLine(agent.objective)
          || (agent.status === "pending" ? "等待启动" : agent.status === "done" ? "执行已完成" : "正在执行");

const resultTitle = (agent: SubagentState): string => {
  if (agent.status === "error") return userVisibleLine(agent.resultError) || "执行失败";
  if (agent.status === "partial") {
    return agent.terminationReason === "deadline_exceeded" ? "达到时限，已保留部分结果" : "部分完成";
  }
  if (agent.status === "cancelled") {
    return agent.terminationInitiator === "user" ? "已停止" : "已取消";
  }
  if (agent.status === "done") return userVisibleLine(agent.summary) || "结果已形成";
  return "等待结果";
};

const isTerminalStatus = (status: SubagentState["status"]): boolean =>
  status === "done" || status === "partial" || status === "cancelled" || status === "error";

/** Canonical delegated-work lifecycle used by Agents, Activity, and future replay projections. */
export function projectSubagentActivityItems(agent: SubagentState): ActivityItem[] {
  const groupKey = `agent:${agent.id}`;
  const phase = phaseFor(agent.role);
  const resultStatus: ActivityItemStatus = agent.status === "done"
    ? "completed"
    : agent.status === "error"
      ? "failed"
      : agent.status === "partial"
        ? "completed"
        : agent.status === "cancelled"
          ? "cancelled"
          : "queued";

  return [
    {
      id: `${agent.id}:created`,
      taskId: agent.taskId,
      agentId: agent.id,
      parentId: agent.parentRunId,
      kind: "agent",
      status: "completed",
      title: "任务已创建",
      // Creation is useful for replay/Inspector, but is not a user-facing
      // milestone. Showing it in the sidebar makes every task look noisier
      // without telling the user what the agent actually did.
      // `developer` is the canonical Inspector-only visibility tier.
      visibility: "developer",
      groupKey,
      hasFailure: false,
      hasPendingUserAction: false,
    },
    {
      id: `${agent.id}:execution`,
      taskId: agent.taskId,
      agentId: agent.id,
      parentId: agent.parentRunId,
      kind: "agent",
      status: executionStatus(agent.status),
      phase,
      title: executionTitle(agent),
      summary: userVisibleLine(agent.objective) || undefined,
      finishedAt: isTerminalStatus(agent.status) ? agent.lastEventAt : undefined,
      visibility: "activity",
      groupKey,
      hasFailure: agent.status === "error",
      hasPendingUserAction: Boolean(agent.needsInput),
    },
    {
      id: `${agent.id}:result`,
      taskId: agent.taskId,
      agentId: agent.id,
      parentId: agent.parentRunId,
      kind: "agent",
      status: resultStatus,
      phase: "finalizing",
      title: resultTitle(agent),
      summary: userVisibleLine(agent.resultContent) || undefined,
      finishedAt: isTerminalStatus(agent.status) ? agent.lastEventAt : undefined,
      visibility: "activity",
      groupKey,
      hasFailure: resultStatus === "failed",
      hasPendingUserAction: false,
    },
  ];
}
