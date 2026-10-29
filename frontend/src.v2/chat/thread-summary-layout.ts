export type ThreadSummaryDisplayMode = "overlay" | "shift" | "gutter";
export type ThreadSummarySurface = "inline" | "popover" | null;

// A 736px transcript shares its pane with the summary card.
export function threadSummaryDisplayMode(width: number): ThreadSummaryDisplayMode {
  const gutter = (width - 736) / 2;
  return gutter < 180 ? "overlay" : gutter < 400 ? "shift" : "gutter";
}

export function threadSummarySurface(
  displayMode: ThreadSummaryDisplayMode,
  pinned: boolean,
  open: boolean,
): ThreadSummarySurface {
  if (!open) return null;
  if (pinned && displayMode !== "overlay") return "inline";
  return displayMode === "overlay" ? "popover" : null;
}
