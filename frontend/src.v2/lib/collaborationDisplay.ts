import type { SubagentState } from "../stores/types";

/**
 * Coordinator guardrail notices (backend/agent/coordinator.py) arrive as raw
 * protocol summaries on blocked subagent rows:
 *   "Coordinator delegation blocked: required delegated results are already
 *    available but not collected (...)"            → collect_results
 *   "Coordinator delegation blocked: similar delegated work already exists
 *    (...)"                                        → duplicate_delegation
 *   "Coordinator delegation blocked: the active delegated-work budget is full
 *    (...)" / "maximum concurrent subagents ..."   → capacity
 *
 * The ordinary UI never shows those sentences; this module classifies them and
 * provides the user-facing replacement copy.
 */
export type CoordinatorNoticeKind = "collect_results" | "duplicate_delegation" | "capacity";

const COORDINATOR_BLOCKED_RE = /coordinator delegation blocked\s*:/i;

const noticeText = (agent: SubagentState): string =>
  [agent.summary, agent.detail, agent.currentActivity]
    .map((value) => String(value || ""))
    .find((value) => COORDINATOR_BLOCKED_RE.test(value)) || "";

/** Classify a coordinator guardrail notice on a subagent row, if any. */
export function coordinatorNoticeKindForSubagent(agent: SubagentState): CoordinatorNoticeKind | null {
  const text = noticeText(agent);
  if (!text) return null;
  if (/already available but not collected|not collected/i.test(text)) return "collect_results";
  if (/similar delegated work already exists|duplicate/i.test(text)) return "duplicate_delegation";
  if (/budget is full|maximum concurrent subagents|capacity/i.test(text)) return "capacity";
  // Unknown blocked reason still comes from the coordinator; treat as capacity
  // (queued) so the raw protocol sentence never leaks into ordinary UI.
  return "capacity";
}

const NOTICE_COPY: Record<CoordinatorNoticeKind, string> = {
  collect_results: "结果正在整理",
  duplicate_delegation: "相同任务已在处理",
  capacity: "任务较多，正在依次处理",
};

/** User-facing replacement copy for a coordinator guardrail notice. */
export function userFacingCoordinatorNoticeForSubagent(agent: SubagentState): string {
  const kind = coordinatorNoticeKindForSubagent(agent);
  return kind ? NOTICE_COPY[kind] : "";
}

/**
 * The status the UI should treat a subagent as being in. Rows carrying a
 * coordinator guardrail notice are protocol-level "blocked" placeholders, and
 * stay blocked; everything else uses the stored lifecycle status directly.
 */
export function effectiveSubagentStatus(agent: SubagentState): SubagentState["status"] {
  if (agent.status === "blocked" || coordinatorNoticeKindForSubagent(agent)) {
    return "blocked";
  }
  return agent.status;
}
