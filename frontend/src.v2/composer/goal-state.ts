import { useSyncExternalStore } from "react";
import { useAppStore } from "../stores";
import type { ConversationGoal } from "../stores/types";
import { toConversationGoal } from "../stores/types";
import type { GoalInfo } from "../protocol/events";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { composerWorkspaceKey } from "../stores/composer-drafts";
import { workspaceRootsEqual } from "../lib/workspace-path";
import type { Translate } from "../i18n";

export interface GoalEditorState {
  draft: string;
  savedObjective: string;
  sourceObjective: string;
  pendingSourceObjective: string | null;
  savedAtMs: number;
  busy: "save" | "status" | "clear" | null;
}

const editors = new Map<string, GoalEditorState>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export const readGoalEditor = (owner: string) => editors.get(owner);
export const updateGoalEditor = (owner: string, patch: Partial<GoalEditorState>) => {
  editors.set(owner, { ...editors.get(owner)!, ...patch });
  listeners.forEach(listener => listener());
};
export const useGoalEditor = (owner: string) => useSyncExternalStore(subscribe, () => editors.get(owner));

export function prepareGoalEditor(owner: string, goal: ConversationGoal) {
  const previous = editors.get(owner);
  if (previous && previous.draft !== previous.savedObjective) return previous;
  const next: GoalEditorState = { draft: goal.text, savedObjective: goal.text, sourceObjective: goal.text,
    pendingSourceObjective: null, savedAtMs: goal.updatedAt ? Date.parse(goal.updatedAt) : Date.now(), busy: null };
  editors.set(owner, next);
  listeners.forEach(listener => listener());
  return next;
}

export function toggleGoalEditor(owner: string, goal: ConversationGoal) {
  const state = useAppStore.getState();
  if (state.rightPanelOpen && state.rightStackTab === "goal") {
    window.dispatchEvent(new CustomEvent("goal:close-editor", { detail: { conversationId: owner } }));
    return;
  }
  prepareGoalEditor(owner, goal);
  state.setRightStackTab("goal");
}

export async function updateThreadGoal(owner: string, action: "set" | "pause" | "resume" | "clear", text?: string) {
  const result = await sendClientCommandAwaitResult({ type: "conversation.goal.set", conversation_id: owner,
    action, ...(text === undefined ? {} : { text }), source: "frontend.goal" }, "goal");
  if (!commandResultSucceeded(result)) throw new Error(result.message);
  const data = result.data as { goal: GoalInfo; revision: number };
  useAppStore.getState().setActiveGoal(toConversationGoal(data.goal), owner, data.revision);
  return result;
}

export async function submitComposerGoal(tr: Translate) {
  const captured = useAppStore.getState();
  const text = captured.draft.trim();
  if (!text) return;
  const owner = captured.conversationId || await captured.ensureComposerConversation();
  if (!owner) return;
  if (captured.activeGoal && captured.activeGoal.text !== text) {
    const { showConfirm } = await import("../overlays/DialogService");
    if (!await showConfirm({ title: tr("替换当前目标？"), message: `${tr("此操作会保留聊天，并用当前输入内容替换已保存的目标。")}\n\n${text}`,
      confirmLabel: tr("替换目标"), cancelLabel: tr("取消") })) return;
  }
  await updateThreadGoal(owner, "set", text);
  useAppStore.setState(state => {
    if (state.conversationId === owner && workspaceRootsEqual(state.workingDirectory, captured.workingDirectory)) {
      return state.draft === captured.draft && state.goalMode ? { draft: "", goalMode: false } : {};
    }
    const workbench = state.conversationWorkbenchStates[owner];
    if (workbench?.draft === captured.draft && workbench.goalMode) return { conversationWorkbenchStates: {
      ...state.conversationWorkbenchStates, [owner]: { ...workbench, draft: "", goalMode: false },
    } };
    const key = composerWorkspaceKey(captured.workingDirectory);
    const workspace = state.workspaceComposerDrafts[key];
    if (!captured.conversationId && workspace?.draft === captured.draft && workspace.goalMode) return { workspaceComposerDrafts: {
      ...state.workspaceComposerDrafts, [key]: { ...workspace, draft: "", goalMode: false },
    } };
    return {};
  });
  window.dispatchEvent(new Event("composer:focus"));
}
