import { useSyncExternalStore } from "react";
import { useAppStore } from "../stores";
import type { ScheduledTaskEntry } from "../stores/types";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { translateApp } from "../i18n";
import type { ThreadActionOwner } from "./thread-actions";

export const openAutomations = () => {
  settingsIntent = null;
  pageState = { ...pageState, open: true, navigationId: pageState.navigationId + 1 };
  listeners.forEach(listener => listener());
  const state = useAppStore.getState();
  if (state.settingsOpen) state.toggleSettings();
  useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app", projectsOpen: false });
};

export const closeAutomations = () => {
  pageState = { ...pageState, open: false };
  listeners.forEach(listener => listener());
};
export const toggleAutomationsSidebar = () => {
  pageState = { ...pageState, sidebarOpen: !pageState.sidebarOpen };
  listeners.forEach(listener => listener());
};

export type ThreadAutomationOwner = Pick<ThreadActionOwner, "id" | "title" | "workspaceRoot" | "permissionMode"> & { schedulerScope?: "global" };
export interface ThreadAutomationEditor {
  id: string;
  owner: ThreadAutomationOwner;
  title?: string;
  task?: ScheduledTaskEntry;
  open: boolean;
}
export interface AutomationSettingsIntent {
  owner: ThreadAutomationOwner;
  task: ScheduledTaskEntry;
}
let editors: ThreadAutomationEditor[] = [];
let settingsIntent: AutomationSettingsIntent | null = null;
let pageState = { open: false, sidebarOpen: true, navigationId: 0 };
const listeners = new Set<() => void>();
const publish = (next: ThreadAutomationEditor[]) => { editors = next; listeners.forEach(listener => listener()); };
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const snapshot = () => editors;
export const useThreadAutomationEditors = () => useSyncExternalStore(subscribe, snapshot);
const settingsSnapshot = () => settingsIntent;
export const useAutomationSettingsIntent = () => useSyncExternalStore(subscribe, settingsSnapshot);
export const getAutomationsPageState = () => pageState;
export const useAutomationsPageState = () => useSyncExternalStore(subscribe, getAutomationsPageState);

export function openThreadAutomationSettings(owner: ThreadAutomationOwner, task: ScheduledTaskEntry) {
  openAutomations();
  settingsIntent = { owner, task };
  listeners.forEach(listener => listener());
}

export async function loadThreadAutomations(owner: ThreadAutomationOwner): Promise<ScheduledTaskEntry[]> {
  const result = await sendClientCommandAwaitResult(owner.schedulerScope === "global"
    ? { type: "scheduler.list", scheduler_scope: "global" }
    : { type: "scheduler.list", owner_conversation_id: owner.id, workspace_root: owner.workspaceRoot }, "scheduler.list");
  if (!commandResultSucceeded(result)) throw new Error(result.message || translateApp("无法读取定时任务。"));
  return (result.data as unknown as { tasks: ScheduledTaskEntry[] }).tasks;
}

export function openThreadAutomationEditor(owner: ThreadAutomationOwner, task?: ScheduledTaskEntry) {
  const existing = editors.find(editor => task ? editor.task?.id === task.id : !editor.task && editor.owner.id === owner.id);
  const id = existing?.id ?? task?.id ?? `draft:${owner.id}`;
  const capturedOwner = { id: owner.id, title: owner.title, workspaceRoot: owner.workspaceRoot, permissionMode: owner.permissionMode };
  publish(existing ? editors.map(editor => editor.id === id ? { ...editor, owner: capturedOwner, task, open: true } : editor)
    : [...editors, { id, owner: capturedOwner, task, open: true }]);
  useAppStore.getState().setRightStackTab(`automation:${id}`);
}

export const closeThreadAutomationEditor = (id: string) => publish(editors.map(editor => editor.id === id ? { ...editor, open: false } : editor));
export const removeThreadAutomationEditor = (id: string) => publish(editors.filter(editor => editor.id !== id));
export const renameThreadAutomationEditor = (id: string, title: string) => publish(editors.map(editor => editor.id === id ? { ...editor, title } : editor));
export const saveThreadAutomationEditor = (id: string, task: ScheduledTaskEntry) => publish(editors.map(editor => editor.id === id ? { ...editor, task } : editor));
