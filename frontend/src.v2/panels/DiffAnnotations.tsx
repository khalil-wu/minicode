import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Plus, UserRound } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import type { DiffLineComment } from "../stores/types";
import { diffFilePathsEqual } from "../chat/diffReviewState";
import { commentWorkspaceKey, diffCommentKey, diffCommentText, type DiffComment, type DiffCommentPosition } from "../lib/diff-comments";
import {
  getDiffCommentScope, readDiffCommentState, subscribeDiffComments,
  saveDiffComment, removeDiffComment, setDiffCommentDraft, removeDiffCommentDraft,
} from "../lib/diff-comment-storage";
import {
  diffCommentHunk, diffCommentPosition, diffSelectionContains,
  type DiffLine, type DiffLineLocation, type DiffLineSelection,
} from "../lib/diff-comment-lines";
import { Button } from "../components/Button";

const endpointKey = (position: DiffCommentPosition) => `${position.side}:${position.line}`;
export const DiffReaderOwnerContext = createContext("");

export function useDiffAnnotations({ lines, rawPatch, filePath, workspaceRoot, conversationId, enabled, sourceKey,
  approvalComments, onApprovalSave, onApprovalRemove,
}: {
  lines: DiffLine[]; rawPatch: string; filePath: string; workspaceRoot?: string; conversationId?: string | null;
  enabled: boolean; approvalComments?: DiffLineComment[];
  sourceKey: string;
  onApprovalSave?: (comment: DiffLineComment, previous?: DiffLineComment) => void;
  onApprovalRemove?: (position: DiffCommentPosition) => void;
}) {
  const currentConversation = useAppStore(state => state.conversationId);
  const workingDirectory = useAppStore(state => state.workingDirectory);
  const sourceWorkspaceRoot = workspaceRoot ?? workingDirectory;
  const workspaceKey = commentWorkspaceKey(sourceWorkspaceRoot);
  const scope = getDiffCommentScope(conversationId === undefined ? currentConversation : conversationId, sourceWorkspaceRoot);
  const snapshot = useSyncExternalStore(subscribeDiffComments, () => readDiffCommentState(scope));
  const ownerId = `${useContext(DiffReaderOwnerContext)}::${sourceKey}`;
  const [selection, setSelection] = useState<DiffLineSelection | null>(null);
  const anchor = useRef<DiffLineLocation | null>(null);
  const dragging = useRef<DiffLineSelection | null>(null);
  const suppressClick = useRef(false);
  const approval = approvalComments !== undefined;
  const comments: DiffComment[] = approval ? approvalComments.map(comment => ({
    type: "comment", content: [{ content_type: "text", text: comment.content }], position: comment.position,
    localDiffHunk: comment.localDiffHunk, localWorkspaceRoot: comment.localWorkspaceRoot,
  })) : snapshot.comments;
  const fileComments = comments.filter(comment => diffFilePathsEqual(comment.position.path, filePath, sourceWorkspaceRoot)
    && (comment.localWorkspaceRoot == null || commentWorkspaceKey(comment.localWorkspaceRoot) === workspaceKey));
  const drafts = Object.values(snapshot.drafts).filter(draft => draft.ownerId === ownerId
    && draft.localWorkspaceRoot != null && commentWorkspaceKey(draft.localWorkspaceRoot) === workspaceKey);

  const clearSelection = () => { setSelection(null); anchor.current = null; dragging.current = null; };
  useEffect(clearSelection, [scope, filePath, rawPatch, workspaceKey]);

  const openSelection = (range: DiffLineSelection) => {
    if (!enabled) return;
    const position = diffCommentPosition(filePath, range);
    setSelection(range);
    if (fileComments.some(comment => endpointKey(comment.position) === endpointKey(position))) return;
    const key = `${ownerId}:${workspaceKey}:${filePath}:${endpointKey(position)}`;
    if (readDiffCommentState(scope).drafts[key]) return;
    setDiffCommentDraft(scope, { key, ownerId, position, text: "", localWorkspaceRoot: sourceWorkspaceRoot,
      localDiffHunk: diffCommentHunk(rawPatch, position) });
  };
  useEffect(() => {
    const release = () => {
      const range = dragging.current;
      if (!range) return;
      dragging.current = null;
      suppressClick.current = true;
      openSelection(range);
    };
    const cancel = () => { dragging.current = null; };
    document.addEventListener("pointerup", release);
    document.addEventListener("pointercancel", cancel);
    return () => { document.removeEventListener("pointerup", release); document.removeEventListener("pointercancel", cancel); };
  });

  const save = (comment: DiffComment, previous?: DiffComment) => {
    if (approval) {
      const old = previous && approvalComments.find(candidate => candidate.position === previous.position);
      onApprovalSave!({ filePath, position: comment.position, content: diffCommentText(comment),
        localDiffHunk: comment.localDiffHunk, localWorkspaceRoot: comment.localWorkspaceRoot }, old);
    } else saveDiffComment(scope, comment, previous);
    clearSelection();
  };
  const remove = (comment: DiffComment) => {
    if (approval) onApprovalRemove!(comment.position);
    else removeDiffComment(scope, diffCommentKey(comment));
    clearSelection();
  };
  const enter = (location: DiffLineLocation) => {
    if (dragging.current) {
      dragging.current = { start: dragging.current.start, end: location };
      setSelection(dragging.current);
    }
  };
  const utility = (location: DiffLineLocation) => enabled ? <DiffCommentUtility location={location}
    selected={diffSelectionContains(lines, selection, location)}
    onPointerDown={event => {
      if (event.button !== 0) return;
      event.preventDefault();
      const start = event.shiftKey && anchor.current ? anchor.current : location;
      anchor.current = start;
      dragging.current = { start, end: location };
      suppressClick.current = false;
      setSelection(dragging.current);
    }}
    onClick={event => {
      if (suppressClick.current) { suppressClick.current = false; return; }
      const start = event.shiftKey && anchor.current ? anchor.current : location;
      anchor.current = start;
      openSelection({ start, end: location });
    }} /> : null;

  const render = (locations: DiffLineLocation[]) => <>
    {fileComments.filter(comment => locations.some(location => location.side === comment.position.side && location.line === comment.position.line))
      .map(comment => {
        const key = `${ownerId}:edit:${diffCommentKey(comment)}`;
        return <DiffCommentCard key={diffCommentKey(comment)} comment={comment} text={snapshot.drafts[key]?.text} disabled={!enabled}
          onTextChange={text => setDiffCommentDraft(scope, { key, ownerId, position: comment.position, text,
            localWorkspaceRoot: comment.localWorkspaceRoot, localDiffHunk: comment.localDiffHunk })}
          onSave={text => { save({ ...comment, content: [{ content_type: "text", text }] }, comment); removeDiffCommentDraft(scope, key); }}
          onClose={() => removeDiffCommentDraft(scope, key)}
          onRemove={() => { remove(comment); removeDiffCommentDraft(scope, key); }} />;
      })}
    {drafts.filter(draft => !draft.key.startsWith(`${ownerId}:edit:`) && locations.some(location => location.side === draft.position.side && location.line === draft.position.line))
      .map(draft => <DiffCommentCard key={draft.key} position={draft.position} text={draft.text} disabled={!enabled}
        onTextChange={text => setDiffCommentDraft(scope, { ...draft, text })}
        onSave={text => {
          save({ type: "comment", content: [{ content_type: "text", text }], position: draft.position,
            localWorkspaceRoot: draft.localWorkspaceRoot, localDiffHunk: draft.localDiffHunk });
          removeDiffCommentDraft(scope, draft.key);
        }}
        onClose={() => { removeDiffCommentDraft(scope, draft.key); clearSelection(); }} />)}
  </>;
  return { utility, enter, render,
    requestChanges: (location?: DiffLineLocation) => { if (location) openSelection({ start: location, end: location }); },
    positions: [...fileComments.map(comment => comment.position), ...drafts.map(draft => draft.position)],
    selected: (location: DiffLineLocation) => diffSelectionContains(lines, selection, location) };
}

export type DiffAnnotations = ReturnType<typeof useDiffAnnotations>;

function DiffCommentUtility({ location, selected, onPointerDown, onClick }: {
  location: DiffLineLocation; selected: boolean;
  onPointerDown: React.PointerEventHandler<HTMLButtonElement>; onClick: React.MouseEventHandler<HTMLButtonElement>;
}) {
  const { t: tr } = useI18n();
  const label = `${location.side === "left" ? "L" : "R"}${location.line}`;
  return <button type="button" className="mc-diff-comment-utility" data-selected={selected || undefined}
    aria-label={tr("评论 {v0}", { v0: label })} title={tr("添加评论 · {v0}", { v0: label })}
    onPointerDown={onPointerDown} onClick={onClick}><Plus size={12} /></button>;
}

function DiffCommentCard({ comment, position = comment!.position, text: controlledText, onTextChange, onSave, onRemove, onClose, disabled = false }: {
  comment?: DiffComment; position?: DiffCommentPosition; text?: string; onTextChange?: (text: string) => void;
  onSave: (text: string) => void; onRemove?: () => void; onClose?: () => void; disabled?: boolean;
}) {
  const { t: tr } = useI18n();
  const [text, setText] = useState(comment ? diffCommentText(comment) : "");
  const [editing, setEditing] = useState(!comment || (controlledText !== undefined && controlledText !== diffCommentText(comment)));
  const value = controlledText ?? text;
  const field = useRef<HTMLTextAreaElement>(null);
  const label = `${position.side === "left" ? "L" : "R"}${position.line}`;
  const start = `${(position.start_side ?? position.side) === "left" ? "L" : "R"}${position.start_line ?? position.line}`;
  const location = start === label ? label : `${start}–${label}`;
  useEffect(() => {
    const element = field.current!;
    element.style.height = "0px";
    element.style.height = `${Math.max(element.scrollHeight, 48)}px`;
  }, [value]);
  const change = (next: string) => { if (onTextChange) onTextChange(next); else setText(next); };
  const cancel = () => {
    if (comment) { change(diffCommentText(comment)); onClose?.(); setEditing(false); field.current?.blur(); }
    else onClose!();
  };
  const submit = () => {
    if (!value.trim()) return;
    onSave(value);
    setEditing(false);
    field.current?.blur();
  };
  return <div className="mc-diff-comment-annotation" data-diff-comment-side={position.side} data-diff-comment-line={position.line}>
    <div className="mc-diff-comment-card">
      <div className="mc-diff-comment-header">
        <span className="mc-diff-comment-avatar" aria-hidden="true"><UserRound size={14} /></span>
        <span>{tr("你")}</span><span className="mc-diff-comment-location">{start === label
          ? tr("第 {line} 行的本地评论", { line: label })
          : tr("第 {startLine} 至 {endLine} 行的本地评论", { startLine: start, endLine: label })}</span>
      </div>
      <textarea ref={field} aria-label={tr("评论 {v0}", { v0: location })} placeholder={tr("添加一条评论…")}
        value={value} readOnly={disabled} autoFocus={!comment} rows={2}
        onChange={event => change(event.target.value)} onFocus={() => setEditing(true)}
        onBlur={event => { if (comment && value === diffCommentText(comment) && !event.currentTarget.parentElement?.contains(event.relatedTarget as Node)) setEditing(false); }}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing || event.keyCode === 229) return;
          if (event.key === "Escape") { event.preventDefault(); cancel(); }
          if (event.key === "Enter" && !event.shiftKey && !disabled) { event.preventDefault(); submit(); }
        }} />
      {!disabled && <div className="mc-diff-comment-footer">
        {onRemove && <Button variant={editing ? "danger" : "ghost"} size="sm" onClick={onRemove}>{tr("删除")}</Button>}
        <span className="flex-1" />
        {editing && <><Button variant="ghost" size="sm" onClick={cancel}>{tr("取消")}</Button>
          <Button variant="primary" size="sm" disabled={!value.trim()} onClick={submit}>{comment ? tr("保存") : tr("评论")}</Button></>}
      </div>}
    </div>
  </div>;
}
