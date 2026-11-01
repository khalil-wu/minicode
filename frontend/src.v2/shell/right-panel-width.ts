// Right panel width bounds for the app shell.
export const rightPanelMaximumWidth = (mainContentWidth: number) => Math.max(320, mainContentWidth - 352);

// Cap the first-run default at 640 and floor it at 320; the height term only
// limits tall-narrow windows. The previous Math.max over both clamps returned
// the larger bound, so a wide window opened with a panel wider than half the
// shell.
export const rightPanelDefaultWidth = (mainContentWidth: number, shellHeight: number) =>
  Math.max(320, Math.min(640, shellHeight * 1.6, mainContentWidth - 500));

export const rightPanelWidthRatio = (width: number, mainContentWidth: number) => {
  const range = rightPanelMaximumWidth(mainContentWidth) - 320;
  return range === 0 ? 0 : Math.max(0, Math.min(1, (width - 320) / range));
};

export const rightPanelWidthFromRatio = (ratio: number, mainContentWidth: number) =>
  320 + ratio * (rightPanelMaximumWidth(mainContentWidth) - 320);
