export type ThreadSummaryDisplayMode = "overlay" | "shift" | "gutter";

// Codex thread-summary-panel-layout: 736px transcript with a 300px summary.
export function threadSummaryDisplayMode(width: number): ThreadSummaryDisplayMode {
  const gutter = (width - 736) / 2;
  return gutter < 180 ? "overlay" : gutter < 400 ? "shift" : "gutter";
}
