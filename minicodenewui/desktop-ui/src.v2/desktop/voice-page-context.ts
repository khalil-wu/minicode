import { useAppStore } from "../stores";

export interface VoicePageContext {
  page: "thread" | "home" | "settings" | "other";
  thread?: { id: string; title: string | null };
  rightSidebarVisible: boolean;
  rightPanelTabs: Array<{ type: string; focused: boolean }>;
}

export function captureVoicePageContext(): VoicePageContext {
  const state = useAppStore.getState();
  const page = state.settingsOpen ? "settings"
    : state.projectsOpen || state.skillsMarketplaceOpen ? "other"
    : state.conversationId ? "thread" : "home";
  const sidebar = document.querySelector<HTMLElement>("#workbench-right-sidebar.mc-sidebar-right");
  const header = sidebar?.querySelector(".mc-sidebar-right-tabs");
  const tabs = header?.querySelectorAll<HTMLElement>(
    '[role="tab"][data-sidebar-tab], .mc-sidebar-browser-tab-host [role="tab"]',
  );
  return {
    page,
    ...(page === "thread" ? { thread: { id: state.conversationId!,
      title: state.conversations.find(thread => thread.id === state.conversationId)?.title ?? null } } : {}),
    rightSidebarVisible: (page === "thread" || page === "home")
      && sidebar?.dataset.open === "true" && sidebar.getAttribute("aria-hidden") !== "true",
    rightPanelTabs: Array.from(tabs ?? [], tab => ({
      type: tab.closest(".mc-sidebar-browser-tab-host") ? "browser" : tab.dataset.sidebarTab!,
      focused: tab.getAttribute("aria-selected") === "true",
    })),
  };
}
