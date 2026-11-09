import { ChevronRight } from "../../lib/icons";
import type { ActivityCellState } from "./cellTypes";
import { GuardianReviewGlyph, GuardianToolReviews } from "./GuardianReviewCell";
import { fileLabel } from "./activityCellHelpers";
import { useI18n } from "../../i18n";
import { useTranscriptReadingPreference } from "../transcriptReadingState";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { useProcessDisclosure } from "../../agent-loop/components/useProcessDisclosure";
import { getToolDiffStats, isToolCallExecuting } from "../../lib/tool-call-reducer";
import { workspaceFilePathsEqual } from "../../lib/workspace-path";
import { InlineDiff } from "../diff/InlineDiff";
import "./guardian-review.css";

/** Pending review: the real file plus the review accessory; unexecuted diffs never count. */
export function GuardianPatchCell({ cell, workspaceRoot }: { cell: ActivityCellState; workspaceRoot: string }) {
  return <div className="activity-cell guardian-patch-cell" data-activity-kind="fileChange" data-guardian-status={cell.automaticApprovalReviews?.at(-1)?.status}>
    {cell.guardianPatchFiles!.map(path => <GuardianPatchFile key={path} cell={cell} path={path} workspaceRoot={workspaceRoot} />)}
  </div>;
}

function GuardianPatchFile({ cell, path, workspaceRoot }: { cell: ActivityCellState; path: string; workspaceRoot: string }) {
  const { t } = useI18n();
  const [preference, setExpanded, userToggled] = useTranscriptReadingPreference(`guardian-patch:${cell.id}:${path}`, false);
  const expanded = useTranscriptSearch() || preference;
  const disclosure = useProcessDisclosure(expanded);
  const reviewing = cell.automaticApprovalReviews?.some(review => review.status === "in_progress");
  const executing = cell.toolCallRecords?.some(isToolCallExecuting);
  const record = cell.toolCallRecords?.find(record => record.diff);
  const diff = record?.diff;
  const file = diff?.files?.find(file => workspaceFilePathsEqual(file.path, path, workspaceRoot));
  const evidence = file ?? (cell.guardianPatchFiles?.length === 1 && diff && !diff.files?.length ? diff : undefined);
  const stats = evidence ? getToolDiffStats(evidence) : undefined;
  const patch = evidence?.patch;
  return <div className="guardian-patch-file" data-guardian-patch-file={path}>
    <button type="button" className="activity-cell-main-button guardian-patch-header" aria-expanded={expanded}
      aria-label={t("{action} {path} 的修改", { action: t(expanded ? "收起" : "展开"), path })}
      onClick={() => { userToggled.current = true; setExpanded(value => !value); }}>
      {cell.guardianDeclined ? <><GuardianReviewGlyph warning /><span>{t("自动审核拒绝了文件更改：{file}", { file: fileLabel(path) })}</span></>
        : <><span className="guardian-patch-file-name" title={path}>
            {!reviewing && executing && <span className="agent-loop-timeline-group-live-title">{t("正在编辑")} </span>}
            {!reviewing && !executing && cell.status === "done" && <>{t("已编辑")} </>}{fileLabel(path)}
          </span>{Boolean(cell.automaticApprovalReviews?.length) && <GuardianReviewGlyph />}</>}
      {stats && <span className="activity-cell-file-change-stats"><span className="activity-cell-added">+{stats.plus}</span><span className="activity-cell-removed">-{stats.minus}</span></span>}
      <ChevronRight size={14} className="mc-process-disclosure guardian-review-chevron" data-expanded={expanded} />
    </button>
    {disclosure.mounted && <div ref={disclosure.element} aria-hidden={!expanded} style={{ overflow: "hidden", pointerEvents: expanded ? "auto" : "none" }}>
      <GuardianToolReviews reviews={cell.automaticApprovalReviews} declined={cell.guardianDeclined} />
      {patch && <InlineDiff patch={patch} contextLines={1} filePath={path} readingKey={`guardian-patch:${cell.id}:${path}:diff`} />}
    </div>}
  </div>;
}
