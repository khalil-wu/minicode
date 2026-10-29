import { streamErrorChevronIcon, streamErrorIcon } from "../../lib/stream-error-icon";
import { RollingNumber } from "../../components/RollingNumber";
import { useI18n } from "../../i18n";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { useTranscriptReadingPreference } from "../transcriptReadingState";
import { useProcessDisclosure } from "../../agent-loop/components/useProcessDisclosure";
import type { ActivityCellState } from "./cellTypes";
import "./StreamErrorCell.css";

/** Source stream-error: muted Wi-Fi row; original failure on disclosure. */
export function StreamErrorCell({ cell }: { cell: ActivityCellState }) {
  const { t } = useI18n();
  const progress = cell.progress!;
  const [preference, setExpanded, userToggled] = useTranscriptReadingPreference(`stream-error:${cell.id}`, !cell.collapsed);
  const expanded = useTranscriptSearch() || preference;
  const disclosure = useProcessDisclosure(expanded);
  const numbered = (progress.retryAttempt ?? 0) > 0 && progress.maxRetries !== undefined;
  const busy = progress.statusCode === 429 || progress.providerErrorCode === "server_is_overloaded";
  const summary = busy ? t("服务器繁忙，正在重连") : numbered ? t("正在重连") : progress.text;
  const details = progress.errorDetails ?? [progress.detail || progress.errorMessage!];
  return <div className="stream-error-cell" data-retry-attempt={progress.retryAttempt}>
    <button type="button" className="stream-error-summary" aria-expanded={expanded}
      onClick={() => { userToggled.current = true; setExpanded(value => !value); }}>
      <span className="stream-error-summary-content">
        <svg width={16} height={16} viewBox={streamErrorIcon.viewBox} fill="currentColor" aria-hidden="true" data-source-icon="wifi-light-16">
          {streamErrorIcon.paths.map((d, index) => <path key={index} d={d} />)}
        </svg>
        <span className="stream-error-label">{summary}{numbered && <> <span className="stream-error-counter"><RollingNumber value={progress.retryAttempt!} />/{progress.maxRetries}</span></>}</span>
      </span>
      <svg width={12} height={12} viewBox={streamErrorChevronIcon.viewBox} fill="currentColor" className="stream-error-chevron mc-process-disclosure" data-expanded={expanded} aria-hidden="true" data-source-icon="stream-error-chevron">
        <path d={streamErrorChevronIcon.path} />
      </svg>
    </button>
    {disclosure.mounted && <div ref={disclosure.element} aria-hidden={!expanded} style={{ pointerEvents: expanded ? "auto" : "none" }}>
      {details.map((detail, index) => <div key={index} className="stream-error-details">{detail}</div>)}
    </div>}
  </div>;
}
