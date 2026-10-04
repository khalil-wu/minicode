import { CircleAlert, Lightbulb, ShieldAlert } from "lucide-react";
import type { ErrorCellState } from "./cellTypes";
import { normalizeAgentErrorMessage, purifyToolErrorText } from "../errorMessages";
import "./cells.css";

/**
 * ErrorCell — dedicated error display.
 *
 * Shows: title + message + recoverability + suggested action.
 * Original diagnostics belong in Inspector, not transcript disclosures.
 * Never hidden inside activity details.
 */
export function ErrorCell({ cell }: { cell: ErrorCellState }) {
  const isPermissionNotice = cell.source === "permission";
  const tone = isPermissionNotice ? "warning" : "danger";
  const purifiedMessage = purifyToolErrorText(cell.message);
  const displayMessage = purifiedMessage
    ? cell.source === "agent" || cell.source === "network"
      ? normalizeAgentErrorMessage(purifiedMessage, { includeProviderDetails: false })
      : purifiedMessage
    : "";
  const ErrorIcon = isPermissionNotice ? ShieldAlert : CircleAlert;

  return (
    <div className={`error-cell error-cell-${tone}`}>
      <div className="error-cell-header">
        <span className={`error-cell-icon error-cell-icon-${tone}`} aria-hidden="true">
          <ErrorIcon size={16} strokeWidth={1.75} />
        </span>
        <span className={`error-cell-title error-cell-title-${tone}`}>{cell.title}</span>
        {!cell.recoverable && (
          <span className="error-cell-fatal-badge">不可恢复</span>
        )}
      </div>

      {displayMessage && (
        <div className="error-cell-message">{displayMessage}</div>
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
