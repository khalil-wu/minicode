import type React from "react";
import type { ErrorCellState } from "./cellTypes";
import "./cells.css";

/**
 * Model-facing markup tags that wrap tool/sandbox errors. These are
 * instructions for the model, not text for the user to read, so strip them
 * from the displayed message (mirrors cc's FallbackToolUseErrorMessage).
 */
const ERROR_MARKUP_TAG_RE = /<\/?(?:tool_use_error|error|sandbox_violation)[^>]*>/gi;

function purifyErrorText(text: string | undefined): string {
  if (!text) return text ?? "";
  const stripped = text.replace(ERROR_MARKUP_TAG_RE, "");
  return stripped === text ? text : stripped.trim();
}

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
  const displayMessage = purifyErrorText(cell.message);

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

      {displayMessage && (
        <div className="error-cell-message">{displayMessage}</div>
      )}

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
