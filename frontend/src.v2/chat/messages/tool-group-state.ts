import type { ViewMode } from "../../stores/types";

export const defaultToolGroupExpanded = ({
  hasFailed,
  hasRunning: _hasRunning,
  viewMode,
}: {
  hasFailed: boolean;
  hasRunning: boolean;
  viewMode: ViewMode;
}): boolean => viewMode === "verbose" || hasFailed;

export const toolGroupIsOpen = ({
  expanded,
  hasRunning: _hasRunning,
  viewMode,
}: {
  expanded: boolean;
  hasRunning: boolean;
  viewMode: ViewMode;
}): boolean => viewMode === "verbose" || expanded;
