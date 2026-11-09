import { useI18n } from "../i18n";
/**
 * Floating review bar for inline agent edits. Presentational only: all block
 * math lives in agent-edit-review*.ts, so this renders counts and buttons and
 * calls back. Keep = accept as-is (dismiss from review); Undo = restore the
 * removed text for the current block.
 */
import { Check, ChevronDown, ChevronUp, Undo2, X } from "../lib/icons";

export interface AgentEditReviewBarProps {
  total: number;
  currentIndex: number;
  currentLine: number | null;
  onReveal: () => void;
  onPrev: () => void;
  onNext: () => void;
  onKeep: () => void;
  onUndo: () => void;
  onKeepAll: () => void;
}

export function AgentEditReviewBar({
  total,
  currentIndex,
  currentLine,
  onReveal,
  onPrev,
  onNext,
  onKeep,
  onUndo,
  onKeepAll,
}: AgentEditReviewBarProps) {
  const { t: tr } = useI18n();
  if (total <= 0) return null;
  const position = Math.min(Math.max(currentIndex + 1, 1), total);
  return (
    <div className="agent-edit-review-bar" role="toolbar" aria-label={tr("代码改动审阅")}>
      <button type="button" className="agent-edit-review-btn agent-edit-review-count" onClick={onReveal} title={tr("定位当前改动 · 第 {v0} 行", { v0: currentLine! })} aria-label={tr("定位第 {v0} 处改动，共 {v1} 处，第 {v2} 行", { v0: position, v1: total, v2: currentLine! })}>
        {position}/{total} · L{currentLine}
      </button>
      <div className="agent-edit-review-group">
        <button type="button" className="agent-edit-review-btn" onClick={onPrev} aria-label={tr("上一处")} title={tr("上一处改动")}>
          <ChevronUp size={14} />
        </button>
        <button type="button" className="agent-edit-review-btn" onClick={onNext} aria-label={tr("下一处")} title={tr("下一处改动")}>
          <ChevronDown size={14} />
        </button>
      </div>
      <div className="agent-edit-review-group">
        <button type="button" className="agent-edit-review-btn agent-edit-review-undo" onClick={onUndo} title={tr("撤销此处改动")}>
          <Undo2 size={14} /> {tr("撤销")}</button>
        <button type="button" className="agent-edit-review-btn agent-edit-review-keep" onClick={onKeep} title={tr("保留此处改动")}>
          <Check size={14} /> {tr("保留")}</button>
      </div>
      <button type="button" className="agent-edit-review-btn agent-edit-review-keepall" onClick={onKeepAll} title={tr("保留全部并关闭审阅")}>
        <X size={14} /> {tr("全部保留")}</button>
    </div>
  );
}
