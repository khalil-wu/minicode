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
    <svg width={18} height={18} viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" data-source-icon="system-error-inline">
      <path d="M8 9.8a.767.767 0 1 1 0 1.533A.767.767 0 0 1 8 9.8Zm0-5.134c.368 0 .667.299.667.667V8a.667.667 0 0 1-1.334 0V5.333c0-.368.299-.667.667-.667Z" />
      <path fillRule="evenodd" d="M8 1.333a6.667 6.667 0 1 1 0 13.334A6.667 6.667 0 0 1 8 1.333Zm0 1.334a5.333 5.333 0 1 0 0 10.666A5.333 5.333 0 0 0 8 2.667Z" clipRule="evenodd" />
    </svg>
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
