import { useEffect, useState } from "react";
import { Pause, Pencil, Play, Target, X } from "../lib/icons";
import { useAppStore } from "../stores";
import { useI18n, type TranslationKey } from "../i18n";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { pushToast } from "../overlays/ToastContainer";
import { prepareGoalEditor, toggleGoalEditor, updateGoalEditor, updateThreadGoal, useGoalEditor } from "./goal-state";
import "./goal.css";

const labels: Record<string, TranslationKey> = { active: "追求目标", paused: "已暂停目标", blocked: "目标受阻",
  usage_limited: "目标用量受限", budget_limited: "目标受预算限制", complete: "目标已达成" };

export function GoalBar() {
  const { t: tr, locale } = useI18n();
  const goal = useAppStore(state => state.activeGoal);
  const enabled = useAppStore(state => capabilityFeatureEnabled(state.runtimeCapabilities, "goals"));
  const owner = useAppStore(state => state.conversationId) || "";
  const disabled = useAppStore(state => !state.isConnected || Boolean(state.pendingConversationSwitchId));
  const editor = useGoalEditor(owner);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!enabled || goal?.status !== "active") return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [enabled, goal?.status]);
  if (!enabled || !goal) return null;
  const busy = Boolean(editor?.busy);
  const targetStatus = goal.status === "active" ? "pause" : ["paused", "blocked", "usage_limited"].includes(goal.status) ? "resume" : null;
  const elapsed = Math.max(0, Math.floor((goal.timeUsedSeconds ?? 0) + (goal.status === "active" && goal.updatedAt ? (now - Date.parse(goal.updatedAt)) / 1000 : 0)));
  const duration = elapsed >= 3600 ? `${Math.floor(elapsed / 3600)}h ${Math.floor(elapsed % 3600 / 60)}m`
    : elapsed >= 60 ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s` : elapsed > 0 ? `${elapsed}s` : "";
  const format = new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 });
  const progress = (goal.status === "active" || goal.status === "budget_limited") && goal.tokenBudget != null
    ? `${format.format(goal.tokensUsed ?? 0)} / ${format.format(goal.tokenBudget)}` : duration;
  const apply = async (action: "pause" | "resume" | "clear") => {
    prepareGoalEditor(owner, goal);
    updateGoalEditor(owner, { busy: action === "clear" ? "clear" : "status" });
    try { await updateThreadGoal(owner, action); }
    catch (error) { pushToast(error instanceof Error ? error.message : tr("目标操作失败"), "error"); }
    finally { updateGoalEditor(owner, { busy: null }); }
  };
  return <div className="composer-goal-row">
    <button type="button" className="composer-goal-summary" disabled={disabled || busy}
      onClick={() => toggleGoalEditor(owner, goal)}>
      <Target size={16} aria-hidden="true" />
      <span className="composer-goal-status">{tr(labels[goal.status] || "目标")}</span>
      <span className="composer-goal-objective" title={goal.text}>{goal.text}</span>
      {progress && <span className="composer-goal-progress">{progress}</span>}
    </button>
    <div className="composer-goal-actions">
      <button type="button" aria-label={tr("清除目标")} title={tr("清除目标")} disabled={disabled || busy} onClick={() => void apply("clear")}><X size={12} /></button>
      {targetStatus && <button type="button" aria-label={tr(targetStatus === "pause" ? "暂停目标" : "继续目标")}
        title={tr(targetStatus === "pause" ? "暂停目标" : "继续目标")} disabled={disabled || busy} onClick={() => void apply(targetStatus)}>
        {targetStatus === "pause" ? <Pause size={12} /> : <Play size={12} />}
      </button>}
      <button type="button" aria-label={tr("编辑目标")} title={tr("编辑目标")} disabled={disabled || busy} onClick={() => toggleGoalEditor(owner, goal)}><Pencil size={12} /></button>
    </div>
  </div>;
}
