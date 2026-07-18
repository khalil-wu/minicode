import { CircleAlert, Lightbulb, ShieldAlert } from "lucide-react";
import type { ErrorCellState } from "./cellTypes";
import { purifyToolErrorText } from "../errorMessages";
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
  const displayMessage = purifyToolErrorText(cell.message);
  const ErrorIcon = isPermissionNotice ? ShieldAlert : CircleAlert;

  return (
    <div className={`error-cell error-cell-${tone}`}>
      <div className="error-cell-header">
        <span className={`error-cell-icon error-cell-icon-${tone}`} aria-hidden="true">
          <ErrorIcon size={16} strokeWidth={1.75} />
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
          <summary className="error-cell-details-summary">技术详情</summary>
          <pre className="error-cell-raw-error">{cell.rawError}</pre>
        </details>
      )}

      {cell.suggestedAction && (
        <div className="error-cell-suggestion">
          <Lightbulb size={14} strokeWidth={1.75} aria-hidden="true" />
          <span>{cell.suggestedAction}</span>
        </div>
      )}
    </div>
  );
}
