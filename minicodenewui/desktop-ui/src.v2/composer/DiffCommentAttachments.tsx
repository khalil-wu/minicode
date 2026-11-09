import { MessageSquareText, X } from "../lib/icons";
import { useEffect, useId, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { diffCommentKey, formatDiffCommentLines, diffCommentText, type DiffComment } from "../lib/diff-comments";
import "./DiffCommentAttachments.css";

export function DiffCommentAttachments({ comments, onRemove, onRemoveAll }: {
  comments: readonly DiffComment[];
  onRemove?: (comment: DiffComment) => void;
  onRemoveAll?: () => void;
}) {
  const { t: tr } = useI18n();
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const previewId = useId();
  useEffect(() => { if (!comments.length) { setOpen(false); setPinned(false); } }, [comments.length]);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) { setOpen(false); setPinned(false); }
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);
  if (!comments.length) return null;
  const label = comments.length === 1 ? tr("1 条评论") : tr("{count} 条评论", { count: comments.length });
  return <div ref={root} className="diff-comment-attachments"
    onPointerEnter={() => setOpen(true)} onPointerLeave={() => { if (!pinned) setOpen(false); }}
    onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget)) { setOpen(false); setPinned(false); }
    }}
    onKeyDown={event => {
      if (event.key === "Escape" && open) {
        event.preventDefault(); event.stopPropagation(); setOpen(false); setPinned(false); trigger.current?.focus();
      }
    }}>
    <span className="diff-comment-attachment-pill">
      <button ref={trigger} type="button" aria-expanded={open} aria-controls={previewId}
        aria-label={tr("查看 {label}", { label })}
        onClick={() => { setOpen(!pinned); setPinned(!pinned); }}>
        <MessageSquareText size={14} aria-hidden="true" /><span>{label}</span>
      </button>
      {onRemoveAll && <button type="button" className="diff-comment-attachment-remove"
        onClick={onRemoveAll} aria-label={tr("移除所有 Diff 评论")}><X size={13} aria-hidden="true" /></button>}
    </span>
    {open && <div id={previewId} className="diff-comment-attachment-preview" role="region" aria-label={tr("Diff 评论预览")}>
      {comments.map(comment => <div className="diff-comment-attachment-entry" key={diffCommentKey(comment)}>
        <div className="diff-comment-attachment-body">
          <div className="diff-comment-attachment-location"><span>{comment.position.path}</span>
            {(!comment.position.start_side || comment.position.start_side === comment.position.side)
              && <span>{comment.position.side === "left" ? "L" : "R"}</span>}
            <span>{formatDiffCommentLines(comment.position)}</span></div>
          <div className="diff-comment-attachment-content">{diffCommentText(comment)}</div>
        </div>
        {onRemove && <button type="button" onClick={() => onRemove(comment)}
          aria-label={tr("移除评论 {path}", { path: comment.position.path })}><X size={13} aria-hidden="true" /></button>}
      </div>)}
    </div>}
  </div>;
}
