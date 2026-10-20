import { useEffect, useRef, useState } from "react";
import { RotateCcw } from "../lib/icons";
import { useAppStore } from "../stores";
import { useI18n } from "../i18n";
import { pushToast } from "../overlays/ToastContainer";
import { prepareGoalEditor, readGoalEditor, updateGoalEditor, updateThreadGoal, useGoalEditor } from "../composer/goal-state";
import "../composer/goal.css";

export function GoalPanel({ onClose, active }: { onClose: () => void; active: boolean }) {
  const { t: tr } = useI18n();
  const owner = useAppStore(state => state.conversationId) || "";
  const goal = useAppStore(state => state.activeGoal);
  const connected = useAppStore(state => state.isConnected && !state.pendingConversationSwitchId);
  const editor = useGoalEditor(owner);
  const input = useRef<HTMLTextAreaElement>(null);
  const previousObjective = useRef(goal?.text);
  const previousStatus = useRef(goal?.status);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (goal && !readGoalEditor(owner)) prepareGoalEditor(owner, goal);
  }, [owner, goal]);
  useEffect(() => {
    if (!goal || goal.status === "complete" && previousStatus.current !== "complete"
      || previousObjective.current !== goal.text && readGoalEditor(owner)?.sourceObjective !== goal.text
        && readGoalEditor(owner)?.pendingSourceObjective !== goal.text) onClose();
    previousObjective.current = goal?.text;
    previousStatus.current = goal?.status;
  }, [owner, goal?.text, goal?.status, onClose]);
  useEffect(() => { if (active) input.current?.focus(); }, [active, Boolean(editor)]);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 60000); return () => clearInterval(timer); }, []);
  if (!goal || !editor) return null;
  const changed = editor.draft !== editor.savedObjective;
  const canSave = connected && !editor.busy && editor.draft.trim().length > 0 && editor.draft.trim() !== editor.savedObjective;
  const save = async () => {
    if (!canSave) return;
    const objective = editor.draft.trim();
    updateGoalEditor(owner, { busy: "save", pendingSourceObjective: objective });
    try {
      await updateThreadGoal(owner, "set", objective);
      updateGoalEditor(owner, { draft: objective, savedObjective: objective, sourceObjective: objective, savedAtMs: Date.now() });
    } catch (error) { pushToast(error instanceof Error ? error.message : tr("保存目标失败"), "error"); }
    finally { updateGoalEditor(owner, { busy: null, pendingSourceObjective: null }); }
  };
  const minutes = Math.max(0, Math.floor((now - editor.savedAtMs) / 60000));
  return <div className="goal-panel" onKeyDown={event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault(); event.stopPropagation(); if (!event.repeat) void save();
    }
  }}>
    <div className="goal-panel-toolbar">
      <span>{minutes === 0 ? tr("刚刚更新") : tr("{minutes} 分钟前更新", { minutes })}</span>
      <button type="button" className="goal-panel-revert" aria-label={tr("还原目标")} title={tr("还原目标")}
        disabled={!changed || Boolean(editor.busy)} onClick={() => updateGoalEditor(owner, { draft: editor.savedObjective })}><RotateCcw size={15} /></button>
      <button type="button" className="goal-panel-save" disabled={!canSave} aria-busy={editor.busy === "save"}
        onClick={() => void save()}>{tr("保存")}</button>
    </div>
    <textarea ref={input} aria-label={tr("目标")} placeholder={tr("目标")} value={editor.draft}
      readOnly={Boolean(editor.busy)} onChange={event => updateGoalEditor(owner, { draft: event.target.value })}
      onKeyDown={event => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void save(); } }} />
  </div>;
}
