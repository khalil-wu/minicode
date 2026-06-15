import type React from "react";
import type { ErrorCellState } from "./cellTypes";
import "./cells.css";

/**
 * ErrorCell — dedicated error display.
 *
 * Shows: title + message + source badge + suggested action.
 * Never hidden inside activity details.
 */
export function ErrorCell({ cell }: { cell: ErrorCellState }) {
  const isPermissionNotice = cell.source === "permission";
  const tone = isPermissionNotice ? "warning" : "danger";
  const sourceLabel =
    cell.source === "tool"
      ? "工具"
      : cell.source === "command"
        ? "命令"
        : cell.source === "permission"
          ? "权限"
          : cell.source === "network"
            ? "网络"
            : "Agent";

  return (
    <div className={`error-cell error-cell-${tone}`}>
      <div className="error-cell-header">
        <span className={`error-cell-icon error-cell-icon-${tone}`}>
          {isPermissionNotice ? "!" : "✕"}
        </span>
        <span className={`error-cell-title error-cell-title-${tone}`}>{cell.title}</span>
        <span className="error-cell-source-badge">{sourceLabel}</span>
        {!cell.recoverable && (
          <span className="error-cell-fatal-badge">不可恢复</span>
        )}
      </div>

      <div className="error-cell-message">{cell.message}</div>

      {cell.rawError && (
        <details className="error-cell-details">
          <summary className="error-cell-details-summary">Developer detail</summary>
          <pre className="error-cell-raw-error">{cell.rawError}</pre>
        </details>
      )}

      {cell.suggestedAction && (
        <div className="error-cell-suggestion">
          💡 {cell.suggestedAction}
        </div>
      )}
    </div>
  );
}
