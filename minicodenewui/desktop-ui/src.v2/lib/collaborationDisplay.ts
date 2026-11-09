import type { SubagentState } from "../stores/types";

export function effectiveSubagentStatus(subagent: SubagentState): SubagentState["status"] {
  return subagent.status;
}
