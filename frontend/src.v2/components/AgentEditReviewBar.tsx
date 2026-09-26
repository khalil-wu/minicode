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
  onPrev: () => void;
  onNext: () => void;
  onKeep: () => void;
  onUndo: () => void;
  onKeepAll: () => void;
}

export function AgentEditReviewBar({
  total,
  currentIndex,
  onPrev,
  onNext,
  onKeep,
  onUndo,
  onKeepAll,
}: AgentEditReviewBarProps) {
  if (total <= 0) return null;
  const position = Math.min(Math.max(currentIndex + 1, 1), total);
  return (
    <div className="agent-edit-review-bar" role="toolbar" aria-label="Agent edit review">
      <span className="agent-edit-review-count">{position}/{total} 处改动</span>
      <div className="agent-edit-review-group">
        <button type="button" className="agent-edit-review-btn" onClick={onPrev} aria-label="上一处" title="上一处改动">
          <ChevronUp size={14} />
        </button>
        <button type="button" className="agent-edit-review-btn" onClick={onNext} aria-label="下一处" title="下一处改动">
          <ChevronDown size={14} />
        </button>
      </div>
      <div className="agent-edit-review-group">
        <button type="button" className="agent-edit-review-btn agent-edit-review-undo" onClick={onUndo} title="撤销此处改动">
          <Undo2 size={14} /> 撤销
        </button>
        <button type="button" className="agent-edit-review-btn agent-edit-review-keep" onClick={onKeep} title="保留此处改动">
          <Check size={14} /> 保留
        </button>
      </div>
      <button type="button" className="agent-edit-review-btn agent-edit-review-keepall" onClick={onKeepAll} title="保留全部并关闭审阅">
        <X size={14} /> 全部保留
      </button>
    </div>
  );
}
