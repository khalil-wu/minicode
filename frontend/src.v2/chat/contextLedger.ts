import type { ContextLedger, ContextLedgerCategory, ContextLedgerEntry } from "../stores/types";

const KNOWN_CATEGORIES: ReadonlySet<string> = new Set([
  "system_runtime",
  "guidelines",
  "skills",
  "files_attachments",
  "history",
  "tool_results",
  "memory",
  "compaction_summaries",
]);

const toNumber = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) ? value : 0;

const normalizeEntry = (value: unknown): ContextLedgerEntry | null => {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const category = String(raw.category || "");
  if (!KNOWN_CATEGORIES.has(category)) return null;
  return {
    category: category as ContextLedgerCategory,
    label: String(raw.label || category),
    estimated_tokens: toNumber(raw.estimated_tokens),
    item_count: toNumber(raw.item_count),
    source_count: toNumber(raw.source_count),
    sources: Array.isArray(raw.sources) ? raw.sources.map((s) => String(s)) : [],
  };
};

/**
 * Normalize the observable context ledger emitted with context_usage /
 * context_compacted events (frontend mirror of backend/agent/context_ledger.py).
 * Returns undefined for absent or malformed payloads so callers can fall back
 * to the previous ledger.
 */
export function normalizeContextLedger(value: unknown): ContextLedger | undefined {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;
  const entries = Array.isArray(raw.entries)
    ? raw.entries.map(normalizeEntry).filter((entry): entry is ContextLedgerEntry => entry !== null)
    : [];
  return {
    schema_version: toNumber(raw.schema_version) || undefined,
    estimated_tokens: toNumber(raw.estimated_tokens),
    actual_tokens: toNumber(raw.actual_tokens),
    compaction_count: toNumber(raw.compaction_count),
    native_attachment_tokens: toNumber(raw.native_attachment_tokens) || undefined,
    native_attachment_count: toNumber(raw.native_attachment_count) || undefined,
    entries,
  };
}
