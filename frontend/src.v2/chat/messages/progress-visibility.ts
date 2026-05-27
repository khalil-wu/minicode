import type { ContentBlock } from "../../stores/types";

export type ProgressRecord = Extract<ContentBlock, { type: "progress" }>;
export type AssistantViewMode = "normal" | "verbose" | "summary";

export function visibleProgressRecords(records: ProgressRecord[], viewMode: AssistantViewMode): ProgressRecord[] {
  const seen = new Set<string>();
  return records
    .filter((record) => record.status !== "info" && record.visibility !== "debug")
    .filter((record) => !(record.stage === "approval" && record.toolName === "ask_user"))
    .filter((record) => {
      if (viewMode === "verbose") return true;
      if (record.stage === "tool") return false;
      return record.visibility !== "compact";
    })
    .filter((record) => {
      const key = `${record.id}:${record.status}:${record.summary || record.message}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}
