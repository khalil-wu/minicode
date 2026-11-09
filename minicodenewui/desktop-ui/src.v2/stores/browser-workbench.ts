import type { AppStore } from "./types";
import { draftWindowOwnerId } from "./draft-window-owner";
import { readLS, writeLS } from "./shared-helpers";
import { safeJsonParse } from "../lib/safe-parse";
import { desktop, type EmbeddedBrowserState } from "../desktop/runtime";

export interface BrowserWorkbenchTab {
  id: string;
  title: string;
  url: string;
  draftUrl: string;
  faviconUrl?: string;
}

type BrowserLayout = Pick<AppStore, "rightPanelOpen" | "rightPanelExpanded" | "rightStackTab" | "rightStackTabLocked">;
export interface BrowserWorkbenchSnapshot {
  tabs: BrowserWorkbenchTab[];
  activeId: string;
  layout?: BrowserLayout;
}
const transferredOwners = new Map<string, string>();
const transferringClosedPages = new Map<string, Set<string>>();
export const currentBrowserOwnerId = (owner: string): string => transferredOwners.get(owner) ?? owner;

export const draftBrowserOwnerId = (draftId: string): string => `client-draft:${draftWindowOwnerId()}:${draftId}`;
export const browserOwnerId = (state: Pick<AppStore, "conversationId" | "activeConversationDraftId" | "conversationDrafts">): string => {
  if (state.conversationId) return state.conversationId;
  const id = state.activeConversationDraftId;
  if (!id) return "";
  const draft = state.conversationDrafts[id];
  return draft?.adopted && draft.conversationId ? draft.conversationId : draftBrowserOwnerId(id);
};

const storageKey = (owner: string) => `minicode.browserWorkbench:${JSON.stringify([draftWindowOwnerId(), owner])}`;
export const readBrowserWorkbench = (owner: string): BrowserWorkbenchSnapshot | null => {
  const saved = readLS(storageKey(owner));
  return saved ? safeJsonParse<BrowserWorkbenchSnapshot | null>(saved, null) : null;
};
export const writeBrowserWorkbench = (owner: string, snapshot: BrowserWorkbenchSnapshot): void => {
  const key = storageKey(owner), encoded = JSON.stringify(snapshot);
  if (readLS(key) !== encoded) writeLS(key, encoded);
};
export const browserWorkbenchLayout = (state: BrowserLayout): BrowserLayout => ({
  rightPanelOpen: state.rightPanelOpen, rightPanelExpanded: state.rightPanelExpanded,
  rightStackTab: state.rightStackTab, rightStackTabLocked: state.rightStackTabLocked,
});
export const saveBrowserWorkbenchLayout = (state: AppStore): void => {
  const owner = browserOwnerId(state);
  if (!owner) return;
  const saved = readBrowserWorkbench(owner);
  if (saved) writeBrowserWorkbench(owner, { ...saved, layout: browserWorkbenchLayout(state) });
};

/** Native events keep their own workbench current even when another chat is selected. */
export const projectBrowserWorkbenchEvent = (event: EmbeddedBrowserState): void => {
  if (!["page", "loading", "updated", "error", "closed"].includes(event.type)) return;
  const saved = readBrowserWorkbench(event.conversationId) ?? { tabs: [], activeId: "" };
  const existing = saved.tabs.find(tab => tab.id === event.id);
  if (event.type === "closed") {
    transferringClosedPages.get(event.conversationId)?.add(event.id);
    const tabs = saved.tabs.filter(tab => tab.id !== event.id);
    writeBrowserWorkbench(event.conversationId, { ...saved, tabs,
      activeId: saved.activeId === event.id ? tabs.at(-1)?.id ?? "" : saved.activeId });
    return;
  }
  const url = event.url === "about:blank" ? "" : event.url || existing?.url || "";
  const tab = { id: event.id, title: event.title || existing?.title || "新标签页", url,
    draftUrl: existing && url === existing.url ? existing.draftUrl : url,
    faviconUrl: event.faviconUrl ?? existing?.faviconUrl };
  const tabs = existing ? saved.tabs.map(value => value.id === event.id ? tab : value) : [...saved.tabs, tab];
  writeBrowserWorkbench(event.conversationId, { ...saved, tabs,
    activeId: !saved.activeId || !existing && event.active !== false ? event.id : saved.activeId });
};

/** Manual browser pages follow the draft only after its real Agent resource exists. */
export const transferDraftBrowserWorkbench = async (draftId: string, conversationId: string): Promise<void> => {
  const fromOwner = draftBrowserOwnerId(draftId);
  const saved = readBrowserWorkbench(fromOwner);
  if (!saved) return;
  const native = desktop();
  const closedPages = new Set<string>();
  transferringClosedPages.set(conversationId, closedPages);
  try {
    if (native) await native.embeddedBrowser.transferOwner({ fromOwner, toOwner: conversationId });
  } finally {
    transferringClosedPages.delete(conversationId);
  }
  transferredOwners.set(fromOwner, conversationId);
  const latest = readBrowserWorkbench(fromOwner)!;
  const target = readBrowserWorkbench(conversationId);
  const existing = new Map(target?.tabs.map(tab => [tab.id, tab]));
  const sourceIds = new Set(latest.tabs.map(tab => tab.id));
  const tabs = [...latest.tabs.filter(tab => !closedPages.has(tab.id)).map(tab => ({ ...tab, ...existing.get(tab.id),
      draftUrl: tab.draftUrl !== tab.url ? tab.draftUrl : existing.get(tab.id)?.draftUrl ?? tab.draftUrl })),
      ...(target?.tabs ?? []).filter(tab => !sourceIds.has(tab.id))];
  writeBrowserWorkbench(conversationId, { ...latest, tabs,
    activeId: tabs.some(tab => tab.id === latest.activeId) ? latest.activeId : tabs.at(-1)?.id ?? "" });
  localStorage.removeItem(storageKey(fromOwner));
};
