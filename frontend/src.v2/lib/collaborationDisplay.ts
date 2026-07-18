import type { SubagentState } from "../stores/types";

export type CoordinatorNoticeKind =
  | "direct_tool"
  | "collect_results"
  | "duplicate_delegation"
  | "capacity"
  | "delegation";

const COORDINATOR_DIRECT_TOOL_RE =
  /Coordinator mode blocks direct tool|all direct tools .* coordinator mode|direct tools are blocked in coordinator mode/i;
const COORDINATOR_DELEGATION_RE = /Coordinator delegation blocked/i;

function combinedText(parts: Array<string | undefined | null>): string {
  return parts.filter(Boolean).join("\n");
}

export function coordinatorNoticeKind(text: string): CoordinatorNoticeKind | null {
  if (COORDINATOR_DIRECT_TOOL_RE.test(text)) return "direct_tool";
  if (!COORDINATOR_DELEGATION_RE.test(text)) return null;
  if (/required delegated results are already available|not collected|workflow outputs|subagent results/i.test(text)) {
    return "collect_results";
  }
  if (/similar delegated work already exists|duplicate worker/i.test(text)) {
    return "duplicate_delegation";
  }
  if (/active delegated-work budget is full|active item\(s\)|budget is full/i.test(text)) {
    return "capacity";
  }
  return "delegation";
}

export function coordinatorNoticeKindForSubagent(subagent: SubagentState): CoordinatorNoticeKind | null {
  return coordinatorNoticeKind(combinedText([
    subagent.summary,
    subagent.detail,
    subagent.currentActivity,
    subagent.resultContent,
    subagent.resultError,
  ]));
}

export function isInternalCoordinatorNotice(text: string): boolean {
  return coordinatorNoticeKind(text) !== null;
}

export function userFacingCoordinatorNotice(kind: CoordinatorNoticeKind | null): string {
  switch (kind) {
    case "direct_tool":
      return "任务缺少必要的读取或搜索能力";
    case "collect_results":
      return "结果正在整理";
    case "duplicate_delegation":
      return "相同任务已在处理中";
    case "capacity":
      return "任务较多，正在依次处理";
    case "delegation":
      return "任务已暂停，请查看现有结果";
    default:
      return "";
  }
}

export function userFacingCoordinatorNoticeForSubagent(subagent: SubagentState): string {
  return userFacingCoordinatorNotice(coordinatorNoticeKindForSubagent(subagent));
}

export function effectiveSubagentStatus(subagent: SubagentState): SubagentState["status"] {
  const kind = coordinatorNoticeKindForSubagent(subagent);
  if (kind === "direct_tool" && subagent.status === "done") return "error";
  if (kind && kind !== "direct_tool" && subagent.status === "done") return "blocked";
  return subagent.status;
}
