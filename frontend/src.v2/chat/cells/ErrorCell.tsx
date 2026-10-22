import { CircleAlert, Lightbulb, ShieldAlert } from "lucide-react";
import type { ErrorCellState } from "./cellTypes";
import { normalizeAgentErrorMessage, purifyToolErrorText } from "../errorMessages";
import "./cells.css";

/**
 * ErrorCell — dedicated error display.
 *
 * Source turn failures are neutral, standalone notices. Permission and tool
 * notices retain their own presentation and original diagnostics.
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

  if (cell.source === "agent" || cell.source === "network") return <div className="error-cell error-cell-notice" role="alert">
    <CircleAlert size={16} strokeWidth={1.75} aria-hidden="true" />
    {displayMessage && <span className="sr-only">{cell.title}</span>}
    <span>{displayMessage || cell.title}{cell.rawError && cell.rawError.trim() !== cell.message.trim() && <><br />{cell.rawError}</>}</span>
  </div>;

  return (
    <div className={`error-cell error-cell-${tone}`}>
      <div className="error-cell-header">
        <span className={`error-cell-icon error-cell-icon-${tone}`} aria-hidden="true">
          <ErrorIcon size={16} strokeWidth={1.75} />
        </span>
        <span className={`error-cell-title error-cell-title-${tone}`}>{cell.title}</span>
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
