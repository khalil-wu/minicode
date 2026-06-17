import type { AgentTimelineItem } from "../types";
import type { RawAgentLoopEvent } from "./raw-events";

const MAX_SORT_SEQ = Number.MAX_SAFE_INTEGER;

export function eventSeq(event: RawAgentLoopEvent, fallback = MAX_SORT_SEQ): number {
  const direct = finiteNumber(event.seq);
  if (direct != null) return direct;
  const order = finiteNumber(event.order);
  if (order != null) return order;
  const created = finiteTimestamp(event.created_at);
  if (created != null) return created;
  const timestamp = finiteTimestamp(event.timestamp);
  if (timestamp != null) return timestamp;
  return fallback;
}

export function sortBySeq<T extends { seq: number; id: string }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => {
    const seqDelta = normalizeSeq(a.seq) - normalizeSeq(b.seq);
    if (seqDelta !== 0) return seqDelta;
    return a.id.localeCompare(b.id);
  });
}

export function sortTimelineItems(items: readonly AgentTimelineItem[]): AgentTimelineItem[] {
  return sortBySeq(items);
}

export function upsertTimelineItem<T extends { id: string; seq: number }>(
  items: readonly T[],
  next: T,
): T[] {
  const index = items.findIndex((item) => item.id === next.id);
  if (index < 0) return sortBySeq([...items, next]);

  const existing = items[index];
  const updated = {
    ...existing,
    ...next,
    seq: Math.min(normalizeSeq(existing.seq), normalizeSeq(next.seq)),
  };
  const copy = items.slice();
  copy[index] = updated;
  return sortBySeq(copy);
}

function normalizeSeq(value: number): number {
  return Number.isFinite(value) ? value : MAX_SORT_SEQ;
}

function finiteNumber(value: unknown): number | null {
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function finiteTimestamp(value: unknown): number | null {
  if (value == null || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const parsed = Date.parse(String(value));
  return Number.isFinite(parsed) ? parsed : null;
}
