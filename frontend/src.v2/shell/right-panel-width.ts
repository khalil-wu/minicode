// Codex CPt / DPt / OPt, app-shell:right-panel-width:v3 (26.1007.2314).
export const rightPanelMaximumWidth = (mainContentWidth: number) => Math.max(320, mainContentWidth - 352);

export const rightPanelDefaultWidth = (mainContentWidth: number, shellHeight: number) =>
  Math.max(320, Math.min(shellHeight * 1.6, mainContentWidth - 500), Math.min(640, mainContentWidth - 352));

export const rightPanelWidthRatio = (width: number, mainContentWidth: number) => {
  const range = rightPanelMaximumWidth(mainContentWidth) - 320;
  return range === 0 ? 0 : Math.max(0, Math.min(1, (width - 320) / range));
};

export const rightPanelWidthFromRatio = (ratio: number, mainContentWidth: number) =>
  320 + ratio * (rightPanelMaximumWidth(mainContentWidth) - 320);
