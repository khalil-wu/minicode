import { ChevronRight, Info } from "../../lib/icons";
import type { GuardianAssessment } from "../../protocol/guardian-types";
import type { GuardianReviewCellState, GuardianWarningCellState } from "./cellTypes";
import { guardianReviewIcon } from "../../lib/guardian-review-icon";
import { useI18n, type Translate } from "../../i18n";
import { useTranscriptReadingPreference } from "../transcriptReadingState";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { useProcessDisclosure } from "../../agent-loop/components/useProcessDisclosure";
import { Tip } from "../../components/Tooltip";
import "./guardian-review.css";

export function GuardianReviewGlyph({ warning = false }: { warning?: boolean }) {
  return <svg width={15} height={15} viewBox={guardianReviewIcon.viewBox} aria-hidden="true" className={`guardian-review-glyph${warning ? " guardian-review-glyph-warning" : ""}`}>
    {guardianReviewIcon.paths.map((path, index) => <path key={index} d={path} fill="currentColor" fillRule="evenodd" clipRule="evenodd" />)}
  </svg>;
}

export function guardianActionSummary(review: GuardianAssessment, t: Translate): string {
  const action = review.action;
  switch (action.type) {
    case "command": return action.command;
    case "write_stdin": return t("向进程 {processId} 发送输入：{stdin}", { processId: action.process_id, stdin: action.stdin });
    case "apply_patch": return action.files.length === 1 ? t("正在编辑 {file}", { file: action.files[0] })
      : t("正在编辑 {fileCount} 个文件", { fileCount: action.files.length });
    case "mcp_tool_call": return t("{connector} 上的 MCP {toolName}", { connector: action.connector_name ?? action.server, toolName: action.tool_name });
    case "network_access": return t("通过网络访问 {target}", { target: action.target });
    case "request_permissions": return action.reason == null ? t("权限请求") : t("权限请求：{reason}", { reason: action.reason });
  }
}

function reviewTitle(review: GuardianAssessment, t: Translate): string {
  switch (review.status) {
    case "in_progress": return t("自动审核中");
    case "approved": return t("自动审核已批准");
    case "denied": return t(review.risk_level === "high" ? "自动审核已拒绝高风险操作" : "自动审核已拒绝");
    case "timed_out": return t("自动审核超时");
    case "aborted": return t("自动审核已停止");
  }
}

function reviewSummary(review: GuardianAssessment, t: Translate): string {
  if (review.rationale?.trim()) return review.rationale;
  switch (review.status) {
    case "in_progress": return t("经过精心提示的审查智能体正在审查此请求，随后 ChatGPT 才会运行它");
    case "aborted": return t("经过精心提示的审查智能体在 ChatGPT 运行此请求前已停止审查此请求");
    case "timed_out": return t("经过精心提示的审查智能体在 ChatGPT 运行此请求前已超时。");
    default: return t("经优化提示的审查智能体已审查此请求。");
  }
}

/** Source automatic-approval-review-details D: initially folded, scan only title. */
export function GuardianReviewDetails({ review }: { review: GuardianAssessment }) {
  const { t } = useI18n();
  const [preference, setExpanded, userToggled] = useTranscriptReadingPreference(`guardian-detail:${review.id}`, false);
  const expanded = useTranscriptSearch() || preference;
  const disclosure = useProcessDisclosure(expanded);
  return <div className="guardian-review-details" data-high-risk={review.status === "denied" && review.risk_level === "high"}>
    <button type="button" className="guardian-review-detail-button" aria-expanded={expanded} onClick={() => { userToggled.current = true; setExpanded(value => !value); }}>
      <span className={review.status === "in_progress" ? "agent-loop-timeline-group-live-title" : undefined}>{reviewTitle(review, t)}</span>
      <ChevronRight size={12} className="mc-process-disclosure guardian-review-chevron" data-expanded={expanded} />
    </button>
    {disclosure.mounted && <div ref={disclosure.element} aria-hidden={!expanded} style={{ overflow: "hidden", pointerEvents: expanded ? "auto" : "none" }}>
      <p className="guardian-review-rationale">{reviewSummary(review, t)}</p>
    </div>}
  </div>;
}

export function GuardianToolReviews({ reviews, declined = false }: { reviews?: GuardianAssessment[]; declined?: boolean }) {
  const { t } = useI18n();
  const visible = reviews?.filter(review => review.status !== "approved") ?? [];
  if (!visible.length) return null;
  if (declined) return <p className="guardian-review-rationale">{t(visible.some(review => review.status === "denied" && review.risk_level === "high")
    ? "此操作被视为高风险，需要明确授权" : "需要明确授权")}</p>;
  return <>{visible.map(review => <GuardianReviewDetails key={review.id} review={review} />)}</>;
}

export function GuardianCommandDenial({ reviews }: { reviews?: GuardianAssessment[] }) {
  const { t } = useI18n();
  const review = reviews?.find(item => item.status === "denied");
  if (!review) return null;
  return <div className="guardian-command-denial"><p>{t("被拒绝的原因")}</p>
    <p className="guardian-review-rationale">{review.rationale?.trim() || t("未提供拒绝原因")}</p></div>;
}

/** Source sites-end-resource aS: action summary opens review details. */
export function GuardianReviewCell({ cell }: { cell: GuardianReviewCellState }) {
  const { t } = useI18n();
  const [preference, setExpanded, userToggled] = useTranscriptReadingPreference(`guardian:${cell.id}`, false);
  const expanded = useTranscriptSearch() || preference;
  const disclosure = useProcessDisclosure(expanded);
  const review = cell.review;
  return <div className="guardian-review-cell" data-guardian-status={review.status}>
    <button type="button" className="guardian-review-action" aria-expanded={expanded} onClick={() => { userToggled.current = true; setExpanded(value => !value); }}>
      <GuardianReviewGlyph warning={review.status === "denied"} />
      <span className={review.status === "in_progress" ? "agent-loop-timeline-group-live-title" : undefined}>{guardianActionSummary(review, t)}</span>
      <ChevronRight size={14} className="mc-process-disclosure guardian-review-chevron" data-expanded={expanded} />
    </button>
    {disclosure.mounted && <div ref={disclosure.element} aria-hidden={!expanded} style={{ overflow: "hidden", pointerEvents: expanded ? "auto" : "none" }}>
      <GuardianToolReviews reviews={[review]} />
    </div>}
  </div>;
}

export function GuardianWarningCell({ cell: _cell }: { cell: GuardianWarningCellState }) {
  const { t } = useI18n();
  const guidance = t("因多次被驳回，自动审查已停止本轮操作。请添加更多上下文或选择其他权限模式以继续。");
  return <div className="guardian-warning-cell"><GuardianReviewGlyph /><span>{t("本轮操作已被自动审查终止")}</span>
    <Tip content={guidance}><button type="button" aria-label={t("任务中断详情")} className="guardian-warning-info"><Info size={12} /></button></Tip>
  </div>;
}
