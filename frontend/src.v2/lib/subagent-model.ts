import type { SubagentState } from "../stores/types";

type ModelPatch = Pick<Partial<SubagentState>, "model" | "provider" | "reasoningEffort">;

/** Only execution metadata can name a child's model; a parent's picker cannot. */
export function subagentModelPatch(payload: Record<string, unknown>, previous?: SubagentState, record?: Record<string, unknown> | null): ModelPatch {
  const epoch = payload.mailbox_epoch ?? payload.mailboxEpoch ?? record?.mailbox_epoch;
  const patch: ModelPatch = typeof epoch === "number" && typeof previous?.mailboxEpoch === "number" && epoch > previous.mailboxEpoch
    ? { model: undefined, provider: undefined, reasoningEffort: undefined } : {};
  for (const [wire, key] of [["model", "model"], ["provider", "provider"], ["reasoning_effort", "reasoningEffort"]] as const) {
    const value = payload[wire] ?? payload[key] ?? record?.[wire] ?? record?.[key];
    if (typeof value === "string") patch[key] = value.trim() || undefined;
  }
  return patch;
}
