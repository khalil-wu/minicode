import { FileDiff } from "lucide-react";
import { useTurnChanges } from "./useTurnChanges";

export function TurnChangeSummary() {
  const { summary, openReview } = useTurnChanges();
  if (!summary) return null;
  return (
    <div className="chat-turn-change-summary">
      <button type="button" onClick={openReview} className="chat-turn-change-button" title="审阅本轮文件更改" aria-label="审阅本轮文件更改">
        <FileDiff size={15} aria-hidden="true" />
        <span>{summary.files.length} 个文件已更改</span>
        <span className="chat-change-added">+{summary.additions}</span>
        <span className="chat-change-deleted">-{summary.deletions}</span>
      </button>
    </div>
  );
}
