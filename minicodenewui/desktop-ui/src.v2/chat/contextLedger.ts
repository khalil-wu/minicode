import type {
  ContextLedger,
  ContextLedgerCategory,
  ContextLedgerEntry,
} from "../stores/types";

const CONTEXT_LEDGER_CATEGORIES = new Set<ContextLedgerCategory>([
  "system_runtime",
  "guidelines",
  "skills",
  "files_attachments",
  "history",
  "tool_results",
  "memory",
  "compaction_summaries",
]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const finiteNonNegativeNumber = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;

const normalizeEntry = (value: unknown): ContextLedgerEntry | null => {
  if (
    !isRecord(value)
    || typeof value.category !== "string"
    || !CONTEXT_LEDGER_CATEGORIES.has(value.category as ContextLedgerCategory)
    || typeof value.label !== "string"
  ) {
    return null;
  }
  const sources = Array.isArray(value.sources)
    ? value.sources.filter((source): source is string => typeof source === "string")
    : [];
  return {
    category: value.category as ContextLedgerCategory,
    label: value.label,
    estimated_tokens: finiteNonNegativeNumber(value.estimated_tokens),
    item_count: finiteNonNegativeNumber(value.item_count),
    source_count: finiteNonNegativeNumber(value.source_count),
    sources,
  };
};

/** Runtime boundary shared by restored snapshots and live context events. */
export const normalizeContextLedger = (value: unknown): ContextLedger | null => {
  if (!isRecord(value) || !Array.isArray(value.entries)) return null;
  return {
    schema_version: 1,
    estimated_tokens: finiteNonNegativeNumber(value.estimated_tokens),
    actual_tokens: finiteNonNegativeNumber(value.actual_tokens),
    compaction_count: finiteNonNegativeNumber(value.compaction_count),
    native_attachment_tokens: finiteNonNegativeNumber(value.native_attachment_tokens),
    native_attachment_count: finiteNonNegativeNumber(value.native_attachment_count),
    entries: value.entries
      .map(normalizeEntry)
      .filter((entry): entry is ContextLedgerEntry => entry !== null),
  };
};
