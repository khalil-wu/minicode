import { useI18n } from "../i18n";
import {
  Bot,
  FileDiff,
  FileSearch,
  FolderOpen,
  Globe2,
  HeartPulse,
  Layers,
  MonitorPlay,
  MessageCirclePlus,
  Plus,
  PanelRightOpen,
  PanelRightClose,
  X,
  TerminalSquare,
  Maximize2,
  Minimize2,
  MessageCircle,
  ChevronDown,
  Target,
} from "../lib/icons";
import { lazy, memo, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { openNewBrowserTab } from "../chat/openWebInBrowser";
import { draftWindowOwnerId } from "../stores/draft-window-owner";
import { RIGHT_SIDEBAR_DEFAULT_WIDTH } from "../stores/shared-helpers";
import { rightPanelDefaultWidth, rightPanelMaximumWidth, rightPanelWidthFromRatio } from "./right-panel-width";
import type { RightStackTab } from "../stores/types";
import { fileWorkspaceScope } from "../protocol/file-workspace";
import { fileIcon } from "../lib/file-icons";
import { workspaceFileTabMenuItems } from "./WorkspaceFileTabs";
import { ContextMenu } from "../components/ContextMenu";
import { SideChatPanel } from "../panels/SideChatPanel";
import type { BrowserTabSummary } from "../panels/BrowserPanel";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import { allowDebugMenu } from "../lib/desktop-debug";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { closeThreadAutomationEditor, useThreadAutomationEditors } from "../lib/automations-navigation";
import { projectAgentViews } from "../lib/agent-view-model";
import { mcpServerNeedsAttention } from "../lib/mcp-status";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { ScrollablePanel } from "./SidebarShared";
import { ActivityTab } from "./tabs/ActivityTab";
import { closeMcpAppPanel, McpAppPanelSlot, mcpAppTabId, useMcpAppSurfaces } from "../chat/mcp-apps/app-surfaces";
import {
  selectActiveConversationPreview,
  selectPreviewSurface,
} from "../lib/preview-projection";

type StackTab = RightStackTab;

interface SidebarRightProps {
  embedded?: boolean;
  visible?: boolean;
  initialTab?: StackTab | "details" | "context";
}

const normalizeInitialTab = (tab: SidebarRightProps["initialTab"]): StackTab => {
  if (tab === "details") return "inspector";
  if (tab === "context") return "tasks";
  if (tab === "plan") return "tasks";
  return tab ?? "tasks";
};

const sidebarIconProps = { size: 16, strokeWidth: 1.5 } as const;

const LazyPreviewPanel = lazy(() =>
  import("../panels/PreviewPanel").then((module) => ({ default: memo(module.PreviewPanel) })),
);
const LazyBrowserPanel = lazy(() =>
  import("../panels/BrowserPanel").then((module) => ({ default: module.BrowserPanel })),
);
const LazyTerminalPanel = lazy(() => import("../panels/TerminalPanel").then(module => ({ default: module.TerminalPanel })));
const LazyFilesPanel = lazy(() => import("../panels/FilesPanel").then(module => ({ default: memo(module.FilesPanel) })));
const LazyFileViewerPanel = lazy(() => import("../panels/FileViewerPanel").then(module => ({ default: memo(module.FileViewerPanel) })));
const LazyGoalPanel = lazy(() => import("../panels/GoalPanel").then(module => ({ default: memo(module.GoalPanel) })));
const LazyDiffPanel = lazy(() =>
  import("../panels/DiffPanel").then((module) => ({ default: memo(module.DiffPanel) })),
);
const LazySubagentsTab = lazy(() =>
  import("./tabs/SubagentsTab").then((module) => ({ default: memo(module.SubagentsTab) })),
);
const LazyArtifactsTab = lazy(() =>
  import("./tabs/ArtifactsTab").then((module) => ({ default: memo(module.ArtifactsTab) })),
);
const LazyInspectorTab = lazy(() =>
  import("./tabs/InspectorTab").then((module) => ({ default: memo(module.InspectorTab) })),
);
const LazyDiagnosticsTab = lazy(() =>
  import("./tabs/DiagnosticsTab").then((module) => ({ default: memo(module.DiagnosticsTab) })),
);
const LazySchedulerTab = lazy(() => import("../overlays/SchedulerTab").then(module => ({ default: module.SchedulerTab })));

export const SidebarRight = ({ embedded = false, visible = true, initialTab }: SidebarRightProps) => {
  const { t: tr } = useI18n();
  const mainConversationTitle = useAppStore((s) => s.conversations.find((item) => item.id === s.conversationId)?.title || tr("主聊天"));
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const conversationId = useAppStore((s) => s.conversationId);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const inspectorFocus = useAppStore((s) => s.inspectorFocus);
  const goalsEnabled = capabilityFeatureEnabled(runtimeCapabilities, "goals");
  const debugAllowed = allowDebugMenu();
  const focusedRecordAvailable = Boolean(inspectorFocus && (inspectorFocus.conversationId ?? conversationId) === conversationId);
  const workspaceFileTabs = useAppStore(s => s.workspaceFileTabs);
  const fileScope = useAppStore(s => fileWorkspaceScope(s));
  const pendingFileTabId = useAppStore(s => s.pendingWorkspaceFileTabId);
  const mcpAppEntries = useMcpAppSurfaces(s => s.entries);
  const automationEditors = useThreadAutomationEditors();
  const mountedAutomationEditors = useRef(new Set<string>());
  for (const editor of automationEditors) if (editor.open && editor.owner.id === conversationId) mountedAutomationEditors.current.add(editor.id);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const rightPanelExpanded = useAppStore((s) => s.rightPanelExpanded);
  const setRightPanelExpanded = useAppStore((s) => s.setRightPanelExpanded);
  const rightSidebarWidth = useAppStore((s) => s.rightSidebarWidth);
  const rightSidebarWidthRatio = useAppStore((s) => s.rightSidebarWidthRatio);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const setRightSidebarWidth = useAppStore((s) => s.setRightSidebarWidth);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);
  const sideChatOpen = useAppStore((s) => s.sideChatOpen);
  const closeSideChat = useAppStore((s) => s.closeSideChat);
  const toggleQuickOpen = useAppStore((s) => s.toggleQuickOpen);
  const shortcutBindings = useAppStore((s) => s.shortcutBindings);
  const [localTab, setLocalTab] = useState<StackTab>(normalizeInitialTab(initialTab));
  const [openTabIds, setOpenTabIds] = useState<StackTab[]>(() => initialTab !== undefined
    ? [normalizeInitialTab(initialTab)]
    : rightPanelOpen ? [normalizeInitialTab(rightStackTab)] : []);
  const [openedPanelPosition, setOpenedPanelPosition] = useState<{ x: number; y: number } | null>(null);
  const [tabMenu, setTabMenu] = useState<{ key: string; x: number; y: number } | null>(null);
  const [tabsOverflow, setTabsOverflow] = useState(false);
  const [viewport, setViewport] = useState({ width: window.innerWidth, height: window.innerHeight });
  const [measuredMainContentWidth, setMeasuredMainContentWidth] = useState<number | null>(null);
  useEffect(() => {
    const resized = () => setViewport({ width: window.innerWidth, height: window.innerHeight });
    window.addEventListener("resize", resized);
    return () => window.removeEventListener("resize", resized);
  }, []);
  const sidebarRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const stage = sidebarRef.current!.closest<HTMLElement>(".workbench-stage");
    if (!stage) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setMeasuredMainContentWidth(entry.contentRect.width);
    });
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);
  const resizeCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => resizeCleanupRef.current?.(), []);
  const tabListRef = useRef<HTMLDivElement | null>(null);
  const tabFocusFrame = useRef<number | null>(null);
  const cancelTabFocus = () => { if (tabFocusFrame.current !== null) window.cancelAnimationFrame(tabFocusFrame.current); tabFocusFrame.current = null; };
  useEffect(() => () => cancelTabFocus(), []);
  const requestedActiveTab = initialTab === undefined ? rightStackTab : localTab;
  const normalizedRequestedTab = requestedActiveTab === "plan" ? "tasks" : requestedActiveTab;
  const activeTab = normalizedRequestedTab;
  const expanded = initialTab === undefined && rightPanelExpanded;
  const canExpand = initialTab === undefined && (activeTab !== "goal" || goalsEnabled) && (["browser", "preview", "diff", "artifacts", "terminal", "sidechat", "files", "subagents", "goal"].includes(activeTab) || activeTab.startsWith("mcp-app:") || activeTab.startsWith("automation:") || activeTab.startsWith("file:"));
  const setActiveTab = initialTab === undefined ? setRightStackTab : setLocalTab;
  useEffect(() => {
    if (!visible) { setOpenedPanelPosition(null); }
  }, [visible]);

  const subagents = useAppStore((s) => s.subagents);
  const activePreviewArtifact = useAppStore((s) => selectActiveConversationPreview(s).previewArtifact);
  const previewSurfaceArtifact = useAppStore((s) => selectPreviewSurface(s).previewArtifact);
  const diffReview = useAppStore((s) => s.diffReview);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const mcpServers = useAppStore((s) => s.mcpServers);
  const runningSubagents = useMemo(() => projectAgentViews(subagents).filter((agent) => agent.status === "running").length, [subagents]);
  const mcpErrors = mcpServers.filter(mcpServerNeedsAttention).length;
  const gitChangeCount = gitChanges.comparison?.files.length
    ?? new Set([...gitChanges.workingTree.map(file => file.path), ...gitChanges.staged.map(file => file.path), ...gitChanges.untracked]).size;

  const tabs: { id: StackTab; label: string; badge?: string; icon: React.ReactNode }[] = [
    ...workspaceFileTabs.filter(tab => tab.open && tab.pane === "right" && (tab.owner.scopeId === fileScope || rightStackTab === `file:${tab.id}`))
      .map(tab => ({ id: `file:${tab.id}` as const, label: tab.title, icon: fileIcon(tab.path, { size: 16 }) })),
    ...automationEditors.filter(editor => editor.open && editor.owner.id === conversationId).map(editor => ({
      id: `automation:${editor.id}` as const, label: editor.title ?? editor.task?.name ?? editor.owner.title,
      icon: <span aria-hidden="true" style={{ display: "inline-block", width: 16, height: 16, background: "currentColor",
        mask: `url(${import.meta.env.BASE_URL}apps/thread-scheduled-task.svg) center / contain no-repeat` }} />,
    })),
    ...mcpAppEntries.filter(entry => entry.scopeId === conversationId && entry.panelOpen).map(entry => ({
      id: mcpAppTabId(entry.app.id), label: entry.app.title, icon: <Layers {...sidebarIconProps} />,
    })),
    { id: "tasks", label: tr("上下文"), icon: <PanelRightOpen {...sidebarIconProps} /> },
    { id: "diff", label: tr("变更"), badge: diffReview ? "1" : gitChangeCount ? String(gitChangeCount) : undefined, icon: <FileDiff {...sidebarIconProps} /> },
    { id: "preview", label: tr("预览"), badge: previewSurfaceArtifact ? tr("开") : undefined, icon: <MonitorPlay {...sidebarIconProps} /> },
    { id: "browser", label: tr("浏览器"), icon: <Globe2 {...sidebarIconProps} /> },
    { id: "terminal", label: tr("终端"), icon: <TerminalSquare {...sidebarIconProps} /> },
    { id: "files", label: tr("文件"), icon: <FolderOpen {...sidebarIconProps} /> },
    ...(goalsEnabled ? [{ id: "goal" as const, label: tr("编辑目标"), icon: <Target {...sidebarIconProps} /> }] : []),
    { id: "sidechat", label: tr("侧边聊天"), icon: <MessageCirclePlus {...sidebarIconProps} /> },
    { id: "artifacts", label: tr("产物"), badge: activePreviewArtifact ? "1" : undefined, icon: <Layers {...sidebarIconProps} /> },
    { id: "subagents", label: tr("子智能体"), badge: runningSubagents ? String(runningSubagents) : undefined, icon: <Bot {...sidebarIconProps} /> },
    ...(debugAllowed || focusedRecordAvailable ? [{ id: "inspector" as const, label: tr(debugAllowed ? "运行详情" : "详情"), icon: <FileSearch {...sidebarIconProps} /> }] : []),
    ...(debugAllowed ? [{ id: "diagnostics" as const, label: tr("运行状态"), badge: mcpErrors ? String(mcpErrors) : undefined, icon: <HeartPulse {...sidebarIconProps} /> }] : []),
  ];
  const [browserTabs, setBrowserTabs] = useState<BrowserTabSummary[]>([]);
  const [browserTabHosts, setBrowserTabHosts] = useState<Record<string, HTMLElement>>({});
  const browserHostCallbacks = useRef(new Map<string, (node: HTMLDivElement | null) => void>());
  const browserHostRef = (id: string) => {
    if (!browserHostCallbacks.current.has(id)) browserHostCallbacks.current.set(id, node => {
      setBrowserTabHosts(current => {
        const next = { ...current };
        if (node) next[id] = node;
        else delete next[id];
        return next;
      });
    });
    return browserHostCallbacks.current.get(id)!;
  };
  const [tabOrder, setTabOrder] = useState<string[]>([]);
  const tabPointerDrag = useRef<{ key: string; pointerId: number; x: number; y: number; active: boolean; element: HTMLDivElement } | null>(null);
  const suppressTabClick = useRef(false);
  const tabById = useMemo(() => new Map(tabs.map((tab) => [tab.id, tab])), [tabs]);
  const openedTabs = openTabIds
    .map((id) => tabById.get(id))
    .filter((tab): tab is { id: StackTab; label: string; badge?: string; icon: React.ReactNode } => Boolean(tab));
  type SharedTab = { key: string; label: string; panel: typeof openedTabs[number]; nativeId?: string };
  const sharedTabs = openedTabs.flatMap<SharedTab>(panel => panel.id === "browser"
    ? browserTabs.map(tab => ({ key: `browser:${tab.id}`, label: tab.title, panel, nativeId: tab.id }))
    : [{ key: panel.id, label: panel.label, panel }]);
  const orderedTabs = [...sharedTabs].sort((left, right) => {
    const position = (tab: SharedTab) => tabOrder.includes(tab.key) ? tabOrder.indexOf(tab.key) : tabOrder.length + sharedTabs.indexOf(tab);
    return position(left) - position(right);
  });
  const sharedTabKeys = sharedTabs.map(tab => tab.key).join("\n");
  const tabMenuFrame = tabMenu ? orderedTabs.find(tab => tab.key === tabMenu.key) : undefined;
  useEffect(() => {
    const live = new Set(sharedTabKeys.split("\n"));
    setTabOrder(current => current.filter(key => live.has(key)));
  }, [sharedTabKeys]);
  const activateSharedTab = (key: string) => {
    const frame = Array.from(tabListRef.current!.querySelectorAll<HTMLElement>("[data-workspace-tab-key]"))
      .find(node => node.dataset.workspaceTabKey === key)!;
    const button = frame.querySelector<HTMLButtonElement>('button[role="tab"]')!;
    button.click(); button.focus();
  };
  const scheduleTabActivation = (key: string) => {
    cancelTabFocus();
    tabFocusFrame.current = window.requestAnimationFrame(() => { tabFocusFrame.current = null; activateSharedTab(key); });
  };
  const finishTabDrag = () => {
    const drag = tabPointerDrag.current;
    if (!drag) return;
    tabPointerDrag.current = null;
    suppressTabClick.current = drag.active;
    if (drag.element.hasPointerCapture(drag.pointerId)) drag.element.releasePointerCapture(drag.pointerId);
  };
  useEffect(() => () => finishTabDrag(), []);
  const tabInteractions = (key: string): React.HTMLAttributes<HTMLDivElement> & { "data-workspace-tab-key": string } => ({
    "data-workspace-tab-key": key,
    style: { touchAction: "none" },
    onMouseDown: cancelTabFocus,
    onContextMenu: event => { event.preventDefault(); event.stopPropagation(); setTabMenu({ key, x: event.clientX, y: event.clientY }); },
    onPointerDown: event => {
      suppressTabClick.current = false;
      if (event.button !== 0) return;
      const button = (event.target as HTMLElement).closest("button");
      if (button && button.getAttribute("role") !== "tab") return;
      tabPointerDrag.current = { key, pointerId: event.pointerId, x: event.clientX, y: event.clientY, active: false, element: event.currentTarget };
    },
    onPointerMove: event => {
      const drag = tabPointerDrag.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      if (!drag.active) {
        if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 5) return;
        drag.active = true;
        drag.element.setPointerCapture(drag.pointerId);
      }
      event.preventDefault(); event.stopPropagation();
      const frames = Array.from(tabListRef.current!.querySelectorAll<HTMLElement>("[data-workspace-tab-key]"));
      const others = frames.filter(frame => frame.dataset.workspaceTabKey !== drag.key);
      const position = others.findIndex(frame => { const bounds = frame.getBoundingClientRect(); return event.clientX <= bounds.left + bounds.width / 2; });
      const keys = others.map(frame => frame.dataset.workspaceTabKey!);
      keys.splice(position < 0 ? keys.length : position, 0, drag.key);
      setTabOrder(current => keys.join("\n") === current.join("\n") ? current : keys);
    },
    onPointerUp: event => { if (tabPointerDrag.current?.active) { event.preventDefault(); event.stopPropagation(); } finishTabDrag(); },
    onPointerCancel: finishTabDrag,
    onLostPointerCapture: finishTabDrag,
    onClickCapture: event => { if (suppressTabClick.current && event.detail > 0) { event.preventDefault(); event.stopPropagation(); } suppressTabClick.current = false; },
    onKeyDown: event => {
      cancelTabFocus();
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>('button[role="tab"]');
      if (!button) return;
      event.preventDefault(); event.stopPropagation();
      const buttons = Array.from(tabListRef.current!.querySelectorAll<HTMLButtonElement>('button[role="tab"]'));
      const current = buttons.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].click(); buttons[next].focus();
    },
  });
  const addOpenTab = useCallback((tab: StackTab) => {
    setOpenTabIds((current) => current.includes(tab) ? current : [...current, tab]);
  }, []);
  const closeOpenTab = useCallback((tab: StackTab, dispose = true) => {
    setTabMenu(null);
    const tabIndex = openTabIds.indexOf(tab);
    if (tabIndex < 0) return;
    if (tab.startsWith("mcp-app:")) closeMcpAppPanel(tab.slice("mcp-app:".length));
    if (tab.startsWith("automation:")) closeThreadAutomationEditor(tab.slice("automation:".length));
    const requested = initialTab === undefined ? useAppStore.getState().rightStackTab : activeTab;
    const liveTabs = requested !== tab && !openTabIds.includes(requested) && useAppStore.getState().rightPanelOpen
      ? [...openTabIds, requested] : openTabIds;
    const nextTabs = liveTabs.filter(id => id !== tab && (!id.startsWith("automation:") || tabById.has(id)));
    setOpenTabIds(nextTabs);
    if (dispose && tab.startsWith("file:")) useAppStore.getState().closeWorkspaceFileTab(tab.slice(5));
    if (tab === "browser") setBrowserTabs([]);
    if (tab === "sidechat") closeSideChat();
    if (nextTabs.length === 0) {
      setActiveTab("tasks");
      if (useAppStore.getState().rightPanelOpen) toggleRightPanel();
      return;
    }
    if (requested !== tab) return;
    const frameIndex = orderedTabs.findIndex(frame => frame.panel.id === tab);
    const remainingFrames = orderedTabs.filter(frame => frame.panel.id !== tab);
    const adjacent = remainingFrames[Math.min(frameIndex, remainingFrames.length - 1)];
    const fallbackTab = adjacent?.panel.id ?? nextTabs[Math.min(tabIndex, nextTabs.length - 1)];
    const panelWasOpen = useAppStore.getState().rightPanelOpen;
    setActiveTab(fallbackTab);
    if (initialTab === undefined && !panelWasOpen && useAppStore.getState().rightPanelOpen) toggleRightPanel();
    if (adjacent) scheduleTabActivation(adjacent.key);
  }, [activeTab, setActiveTab, openTabIds, orderedTabs, tabById, closeSideChat, rightPanelOpen, toggleRightPanel, initialTab]);
  const closeSharedTabs = (keys: string[]) => {
    setTabMenu(null);
    const closing = orderedTabs.filter(tab => keys.includes(tab.key));
    for (const tab of closing) {
      if (tab.nativeId) browserTabHosts[tab.nativeId].querySelector<HTMLButtonElement>('button:not([role="tab"])')!.click();
      else {
        if (tab.panel.id.startsWith("file:")) useAppStore.getState().closeWorkspaceFileTab(tab.panel.id.slice(5));
        if (tab.panel.id.startsWith("mcp-app:")) closeMcpAppPanel(tab.panel.id.slice(8));
        if (tab.panel.id.startsWith("automation:")) closeThreadAutomationEditor(tab.panel.id.slice(11));
        if (tab.panel.id === "sidechat") closeSideChat();
      }
    }
    const closedPanels = new Set(closing.filter(tab => !tab.nativeId).map(tab => tab.panel.id));
    setOpenTabIds(current => current.filter(id => !closedPanels.has(id)));
    const remaining = orderedTabs.filter(tab => !keys.includes(tab.key));
    if (closing.some(tab => tab.panel.id === activeTab)) {
      if (remaining.length) { setActiveTab(remaining[0].panel.id); scheduleTabActivation(remaining[0].key); }
      else { setActiveTab("tasks"); if (useAppStore.getState().rightPanelOpen) toggleRightPanel(); }
    }
  };
  useEffect(() => {
    if (!pendingFileTabId) return;
    const tab = workspaceFileTabs.find(value => value.id === pendingFileTabId)!;
    const ownerId = tab.owner.scopeId.startsWith("conversation:") ? tab.owner.scopeId.slice(13) : tab.owner.conversationId;
    if (ownerId !== conversationId || useAppStore.getState().pendingConversationSwitchId) return;
    useAppStore.setState({ pendingWorkspaceFileTabId: null });
    useAppStore.getState().activateWorkspaceFileTab(tab.id);
  }, [pendingFileTabId, conversationId, workspaceFileTabs]);
  useEffect(() => {
    const ids = workspaceFileTabs.filter(tab => tab.open && tab.pane === "right" && tab.owner.scopeId === fileScope).map(tab => `file:${tab.id}` as StackTab);
    setOpenTabIds(current => [...current.filter(id => !id.startsWith("file:") || tabById.has(id)), ...ids.filter(id => !current.includes(id))]);
  }, [workspaceFileTabs, fileScope]);
  const closeGoalEditor = useCallback(() => closeOpenTab("goal"), [closeOpenTab]);
  useEffect(() => {
    const close = (event: Event) => {
      if ((event as CustomEvent<{ conversationId: string }>).detail.conversationId === conversationId) closeGoalEditor();
    };
    window.addEventListener("goal:close-editor", close);
    return () => window.removeEventListener("goal:close-editor", close);
  }, [conversationId, closeGoalEditor]);
  const nativeTabClosed = (id: string, wasActive: boolean) => {
    if (!wasActive || (initialTab === undefined ? useAppStore.getState().rightStackTab : activeTab) !== "browser") return;
    const index = orderedTabs.findIndex(tab => tab.nativeId === id);
    const remaining = orderedTabs.filter(tab => tab.nativeId !== id);
    if (remaining.length === 0) { closeOpenTab("browser"); return; }
    const adjacent = remaining[Math.min(index, remaining.length - 1)];
    setActiveTab(adjacent.panel.id);
    scheduleTabActivation(adjacent.key);
  };
  useEffect(() => {
    if (initialTab !== undefined) return;
    const run = (event: Event) => {
      const state = useAppStore.getState();
      const numbered = event.type === "select-numbered-tab" ? (event as CustomEvent<{ index: number; conversationId: string | null; surface?: "sidebar" }>).detail : null;
      if (numbered && numbered.conversationId !== state.conversationId) return;
      const workspaceVisible = !state.settingsOpen && !state.skillsMarketplaceOpen && !state.projectsOpen;
      const inSidebar = sidebarRef.current?.contains(document.activeElement);
      const editorFocused = workspaceVisible && numbered?.surface !== "sidebar" && state.appMode === "code" && !inSidebar
        && state.panelSlots.find(slot => slot.focused)?.kind === "editor";
      if (editorFocused && state.activeTabPath) {
        if (numbered) { const tab = state.editorTabs[numbered.index]; if (tab) state.setActiveTab(tab.path); return; }
        if (event.type === "close-tab") window.dispatchEvent(new Event("editor:close-tab"));
        else {
          const index = state.editorTabs.findIndex(tab => tab.path === state.activeTabPath);
          const next = (index + (event.type === "next-tab" ? 1 : -1) + state.editorTabs.length) % state.editorTabs.length;
          state.setActiveTab(state.editorTabs[next].path);
        }
        return;
      }
      if (workspaceVisible && state.rightPanelOpen && orderedTabs.length > 0) {
        if (numbered) { const tab = orderedTabs[numbered.index]; if (tab) activateSharedTab(tab.key); return; }
        const index = orderedTabs.findIndex(tab => tab.panel.id === state.rightStackTab
          && (!tab.nativeId || browserTabs.find(browser => browser.id === tab.nativeId)?.active));
        const tab = orderedTabs[index];
        if (event.type === "close-tab") {
          if (tab.nativeId) browserTabHosts[tab.nativeId].querySelector<HTMLButtonElement>('button:not([role="tab"])')!.click();
          else closeOpenTab(tab.panel.id);
        } else {
          const next = (index + (event.type === "next-tab" ? 1 : -1) + orderedTabs.length) % orderedTabs.length;
          scheduleTabActivation(orderedTabs[next].key);
        }
        return;
      }
      if (event.type === "close-tab") void window.__MINICODE_RUNTIME__?.desktop?.windowControls.close();
    };
    window.addEventListener("previous-tab", run);
    window.addEventListener("next-tab", run);
    window.addEventListener("close-tab", run);
    window.addEventListener("select-numbered-tab", run);
    return () => {
      window.removeEventListener("previous-tab", run);
      window.removeEventListener("next-tab", run);
      window.removeEventListener("close-tab", run);
      window.removeEventListener("select-numbered-tab", run);
    };
  }, [initialTab, visible, orderedTabs, browserTabs, browserTabHosts, closeOpenTab]);
  useEffect(() => {
    if (rightPanelOpen && (activeTab !== "sidechat" || sideChatOpen)) addOpenTab(activeTab);
  }, [activeTab, addOpenTab, rightPanelOpen, sideChatOpen]);

  useEffect(() => {
    if (sideChatOpen) {
      addOpenTab("sidechat");
      setActiveTab("sidechat");
    }
  }, [sideChatOpen, addOpenTab, setActiveTab]);
  useEffect(() => {
    if (!sideChatOpen && openTabIds.includes("sidechat")) closeOpenTab("sidechat");
  }, [sideChatOpen, openTabIds, closeOpenTab]);

  useEffect(() => {
    if (activeTab.startsWith("mcp-app:") && openTabIds.includes(activeTab)
        && !mcpAppEntries.some(entry => mcpAppTabId(entry.app.id) === activeTab && entry.scopeId === conversationId && entry.panelOpen)) {
      closeOpenTab(activeTab);
    }
  }, [activeTab, closeOpenTab, conversationId, mcpAppEntries, openTabIds]);

  useLayoutEffect(() => {
    if (!embedded && !rightPanelOpen) {
      for (const entry of mcpAppEntries) {
        if (entry.scopeId === conversationId && entry.panelOpen) closeMcpAppPanel(entry.app.id);
      }
    }
  }, [conversationId, embedded, mcpAppEntries, rightPanelOpen]);

  useLayoutEffect(() => {
    const track = tabListRef.current!;
    const revealActive = () => {
      track.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.closest<HTMLElement>("[data-workspace-tab-key]")
        ?.scrollIntoView?.({ inline: "nearest", block: "nearest" });
      setTabsOverflow(track.scrollWidth > track.clientWidth);
    };
    revealActive();
    const observer = new ResizeObserver(revealActive);
    observer.observe(track);
    return () => observer.disconnect();
  }, [activeTab, openTabIds, browserTabs, browserTabHosts, tabOrder]);

  useEffect(() => {
    if (diffReview || gitChangeCount > 0) addOpenTab("diff");
  }, [addOpenTab, diffReview, gitChangeCount]);

  useEffect(() => {
    if (previewSurfaceArtifact) addOpenTab("preview");
  }, [addOpenTab, previewSurfaceArtifact]);

  const mainContentWidth = measuredMainContentWidth ?? viewport.width - leftSidebarWidth;
  const minSidebarWidth = embedded ? 0 : 320;
  const effectiveResizeMin = minSidebarWidth;
  const effectiveResizeMax = rightPanelMaximumWidth(mainContentWidth);
  const defaultSidebarWidth = rightPanelDefaultWidth(mainContentWidth, viewport.height);
  const boundWidth = (width: number) => Math.min(effectiveResizeMax, Math.max(effectiveResizeMin, width));
  const sidebarWidth = boundWidth(rightSidebarWidthRatio === null
    ? rightSidebarWidth === RIGHT_SIDEBAR_DEFAULT_WIDTH ? defaultSidebarWidth : rightSidebarWidth
    : rightPanelWidthFromRatio(rightSidebarWidthRatio, mainContentWidth));
  const sidebarWidthStyle = expanded || embedded ? "100%" : rightPanelOpen ? `${sidebarWidth}px` : "0px";
  const activateTab = (tab: StackTab) => {
    addOpenTab(tab);
    setActiveTab(tab);

    setOpenedPanelPosition(null);
  };

  const closeRightPanel = () => {
    // The close control lives inside the panel. Blur it before the next render
    // marks the aside aria-hidden; otherwise Chromium retains focus in a
    // hidden subtree and reports a real accessibility violation.
    const active = document.activeElement;
    if (active instanceof HTMLElement && sidebarRef.current?.contains(active)) {
      active.blur();
    }
    toggleRightPanel();
  };

  useLayoutEffect(() => {
    if (embedded || rightPanelOpen) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && sidebarRef.current?.contains(active)) {
      active.blur();
    }
  }, [embedded, rightPanelOpen]);

  useLayoutEffect(() => {
    if (!sidebarRef.current) return;
    (sidebarRef.current as HTMLElement & { inert: boolean }).inert = !visible || (!embedded && !rightPanelOpen);
  }, [embedded, rightPanelOpen, visible]);


  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (embedded) return;
    event.preventDefault();
    resizeCleanupRef.current?.();
    const handle = event.currentTarget;
    const pointerId = event.pointerId;
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    const previouslyDragging = document.body.classList.contains("layout-dragging");
    const startX = event.clientX;
    const startWidth = sidebarWidth;
    let nextWidth = startWidth;
    let frame: number | null = null;
    const onMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      nextWidth = boundWidth(startWidth + startX - moveEvent.clientX);
      if (frame === null) frame = requestAnimationFrame(() => {
        frame = null;
        setRightSidebarWidth(nextWidth, { mainContentWidth, persist: false });
      });
    };
    const cleanup = () => {
      if (frame !== null) cancelAnimationFrame(frame);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      handle.removeEventListener("lostpointercapture", finishResize);
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      if (!previouslyDragging) document.body.classList.remove("layout-dragging");
      resizeCleanupRef.current = null;
    };
    const finishResize = () => {
      setRightSidebarWidth(nextWidth, { mainContentWidth });
      cleanup();
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      finishResize();
    };
    resizeCleanupRef.current = cleanup;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("layout-dragging");
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    handle.addEventListener("lostpointercapture", finishResize);
  };

  const resetSidebarWidth = () => {
    if (embedded) return;
    setRightSidebarWidth(defaultSidebarWidth, { mainContentWidth });
  };
  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16;
    let nextWidth: number | null = null;
    if (event.key === "ArrowLeft") nextWidth = sidebarWidth + step;
    else if (event.key === "ArrowRight") nextWidth = sidebarWidth - step;
    else if (event.key === "Home") nextWidth = effectiveResizeMin;
    else if (event.key === "End") nextWidth = effectiveResizeMax;
    else if (event.key === "Enter") nextWidth = defaultSidebarWidth;
    if (nextWidth == null) return;
    event.preventDefault();
    setRightSidebarWidth(boundWidth(nextWidth), { mainContentWidth });
  };

  return (
    <aside
      ref={sidebarRef}
      id="workbench-right-sidebar"
      className="mc-sidebar-right relative flex flex-col"
      data-embedded={embedded ? "true" : "false"}
      data-expanded={expanded ? "true" : "false"}
      data-open={visible && (embedded || rightPanelOpen) ? "true" : "false"}
      aria-hidden={!visible || (!embedded && !rightPanelOpen)}
      style={{
        "--right-sidebar-width": `${sidebarWidth}px`,
        position: "relative",
        alignSelf: "stretch",
        margin: 0,
        zIndex: embedded ? undefined : "var(--z-content)",
        display: "flex",
        flexDirection: "column",
        overflow: "visible",
        width: sidebarWidthStyle,
        minWidth: 0,
        flex: expanded || embedded ? "1 1 auto" : "0 0 auto",
        background: "var(--surface-base)",
        border: 0,
        borderLeft: expanded || embedded || !rightPanelOpen ? 0 : "1px solid var(--border-subtle)",
        borderRadius: 0,
        boxShadow: "none",
        opacity: embedded || rightPanelOpen ? 1 : 0,
        transform: embedded || rightPanelOpen ? "none" : "translateX(8px)",
        visibility: embedded || rightPanelOpen ? "visible" : "hidden",
        pointerEvents: embedded || rightPanelOpen ? "auto" : "none",
        transition: `width var(--transition-normal), opacity var(--transition-fast), transform var(--duration-base) var(--easing-enter), visibility 0s linear ${embedded || rightPanelOpen ? "0ms" : "var(--duration-base)"}`,
      } as React.CSSProperties}
    >
      {!embedded && !expanded && (
        <div
          className="mc-sidebar-right-resize-handle"
          role="separator"
          aria-orientation="vertical"
          aria-label={tr("调整右侧栏宽度")}
           aria-valuemin={effectiveResizeMin}
          aria-valuemax={effectiveResizeMax}
           aria-valuenow={Math.round(sidebarWidth)}
           aria-valuetext={tr("{count} 像素", { count: Math.round(sidebarWidth) })}
           tabIndex={0}
          title={tr("调整侧栏宽度")}
          onPointerDown={startResize}
           onDoubleClick={resetSidebarWidth}
           onKeyDown={handleResizeKeyDown}
          style={resizeHandleStyle}
        />
      )}
      <div className="mc-sidebar-right-content">
      <div className="mc-sidebar-right-header">
        {expanded && <button type="button" className="mc-sidebar-return-chat" aria-label={tr("返回主聊天")} title={tr("返回主聊天")}
          onClick={() => setRightPanelExpanded(false)}><MessageCircle size={15} /><span>{mainConversationTitle}</span></button>}
        <div ref={tabListRef} className="mc-sidebar-right-tabs" role="tablist" aria-label={tr("右侧栏面板")}>
        {orderedTabs.map(({ panel: t, nativeId, key }) => nativeId ? <div key={key} ref={browserHostRef(nativeId)} className="mc-sidebar-browser-tab-host" /> : (
          <div
            key={key}
            {...tabInteractions(key)}
            className="mc-sidebar-right-tab-frame"
            data-active={activeTab === t.id ? "true" : "false"}
            data-sidebar-tab-frame={t.id}
          >
            <button
              type="button"
              role="tab"
              id={`right-tab-${t.id}`}
              aria-selected={activeTab === t.id}
              tabIndex={activeTab === t.id ? 0 : -1}
              aria-controls={`right-panel-${t.id}`}
              data-sidebar-tab={t.id}
              onClick={() => activateTab(t.id)}
              title={t.label}
              aria-label={tr("打开{label}", { label: t.label })}
              className="mc-sidebar-right-tab"
            >
              <span className="mc-sidebar-tab-icon">{t.icon}</span>
              <span className="mc-sidebar-tab-label">{t.label}</span>
              {t.badge && <span className="mc-sidebar-tab-badge">{t.badge}</span>}
            </button>
            {(
              <button
                type="button"
                title={tr("关闭{label}标签页", { label: t.label })}
                aria-label={tr("关闭{label}标签页", { label: t.label })}
                onClick={(event) => {
                  event.stopPropagation();
                  closeOpenTab(t.id);
                }}
                className="mc-sidebar-right-tab-close mc-icon-button mc-icon-button-compact"
              >
                <X size={14} />
              </button>
            )}
          </div>
        ))}
        </div>
        <div className="mc-sidebar-right-actions" data-testid="right-sidebar-actions">
          {tabsOverflow && <button
            type="button"
            className="mc-sidebar-right-action mc-icon-button"
            aria-label={tr("已打开的面板")}
            title={tr("已打开的面板")}
            aria-haspopup="menu"
            aria-expanded={openedPanelPosition !== null}
            onClick={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();

              setOpenedPanelPosition((position) => position ? null : { x: Math.max(8, rect.right - 240), y: rect.bottom + 6 });
            }}
          ><ChevronDown size={15} /></button>}
          {canExpand && <button
            type="button"
            className="mc-sidebar-right-action mc-icon-button"
            aria-label={expanded ? tr("还原到侧栏") : tr("扩大到主工作区")}
            title={expanded ? tr("还原到侧栏") : tr("扩大到主工作区")}
            aria-pressed={expanded}
            onClick={() => setRightPanelExpanded(!expanded)}
          >{expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>}
          <button
            type="button"
            onClick={() => { setOpenedPanelPosition(null); openNewBrowserTab(); }}
            title={tr("新建标签页")}
            aria-label={tr("新建标签页")}
            className="mc-sidebar-right-action mc-sidebar-right-action-add mc-icon-button"
          >
            <Plus size={18} strokeWidth={1.8} />
          </button>
          {!embedded && (
            <button
            type="button"
            onClick={closeRightPanel}
            title={tr("关闭面板")}
            aria-label={tr("关闭右侧栏")}
            className="mc-sidebar-right-action mc-icon-button"
          >
              <PanelRightClose size={16} strokeWidth={1.8} />
            </button>
          )}
        </div>
      </div>
      {openedPanelPosition && <ContextMenu
        position={openedPanelPosition}
        onClose={() => setOpenedPanelPosition(null)}
        items={orderedTabs.map((tab) => ({
          label: `${tab.label}${activeTab === tab.panel.id && (!tab.nativeId || browserTabs.find(item => item.id === tab.nativeId)?.active) ? tr(" · 当前") : ""}`,
          icon: tab.panel.icon,
          onClick: () => activateSharedTab(tab.key),
        }))}
      />}
      {tabMenu && tabMenuFrame && (() => {
        const frame = tabMenuFrame;
        const file = frame.panel.id.startsWith("file:") ? workspaceFileTabs.find(tab => `file:${tab.id}` === frame.panel.id) : undefined;
        const right = orderedTabs.slice(orderedTabs.indexOf(frame) + 1).map(tab => tab.key);
        const close = () => closeSharedTabs([frame.key]);
        const closeOthers = () => closeSharedTabs(orderedTabs.filter(tab => tab.key !== frame.key).map(tab => tab.key));
        const closeRight = () => closeSharedTabs(right);
        return <ContextMenu position={tabMenu} ariaLabel={tr("标签页操作")} onClose={() => setTabMenu(null)} items={file ? workspaceFileTabMenuItems(file, tr, {
          close, closeOthers, closeRight, otherCount: orderedTabs.length - 1, rightCount: right.length,
          move: pane => { useAppStore.getState().moveWorkspaceFileTab(file.id, pane); if (pane === "left") closeOpenTab(frame.panel.id, false); },
        }) : [
          { label: tr("关闭"), onClick: close },
          { label: tr("关闭其他标签页"), disabled: orderedTabs.length === 1, onClick: closeOthers },
          { label: tr("关闭右侧标签页"), disabled: right.length === 0, onClick: closeRight },
        ]} />;
      })()}
      {openedTabs.filter(({ id }) => !id.startsWith("automation:")).map(({ id: panelTab }) => <div
        key={panelTab}
        className="mc-sidebar-panel-view flex-1 overflow-hidden text-sm flex flex-col"
        id={`right-panel-${panelTab}`}
        role="tabpanel"
        aria-labelledby={panelTab === "browser" ? `right-browser-tab-${browserTabs.find(tab => tab.active)?.id}` : `right-tab-${panelTab}`}
        hidden={activeTab !== panelTab}
        style={{ display: activeTab === panelTab ? "flex" : "none" }}
      >
        <div className="panel-content-wrapper" style={{ minHeight: 0, minWidth: 0, overflow: "hidden" }}>
          {panelTab.startsWith("mcp-app:") && <McpAppPanelSlot id={panelTab.slice("mcp-app:".length)} />}
          {panelTab.startsWith("file:") && <Suspense fallback={<PanelSkeleton kind="preview" />}><LazyFileViewerPanel tabId={panelTab.slice(5)} active={visible && rightPanelOpen && activeTab === panelTab} /></Suspense>}
          {panelTab === "sidechat" && sideChatOpen && <SideChatPanel active={visible && activeTab === "sidechat" && rightPanelOpen} />}
          {panelTab === "preview" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="preview" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName={tr("预览")} />}>
                  <LazyPreviewPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "browser" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="browser" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName={tr("浏览器")} />}>
                  <LazyBrowserPanel tabHosts={browserTabHosts} tabInteractions={id => tabInteractions(`browser:${id}`)} onTabsChanged={setBrowserTabs} onTabClosed={nativeTabClosed} onLastTabClosed={() => closeOpenTab("browser")} />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "terminal" && rightPanelOpen && <Suspense fallback={<PanelSkeleton kind="terminal" />}>
            <LazyTerminalPanel visible={visible && activeTab === "terminal"} />
          </Suspense>}
          {panelTab === "files" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><LazyFilesPanel active={visible && rightPanelOpen && activeTab === "files"} /></Suspense>}
          {panelTab === "goal" && goalsEnabled && <Suspense fallback={<PanelSkeleton kind="inspector" />}><LazyGoalPanel key={conversationId || ""}
            active={visible && rightPanelOpen && activeTab === "goal"} onClose={closeGoalEditor} /></Suspense>}
          {panelTab === "tasks" && <ScrollablePanel><ActivityTab /></ScrollablePanel>}
          {panelTab === "diff" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="diff" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName={tr("变更")} />}>
                  <LazyDiffPanel readerOwnerId={`changes:${draftWindowOwnerId()}`} />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "subagents" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="subagents" />}>
                <ScrollablePanel><LazySubagentsTab /></ScrollablePanel>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {panelTab === "artifacts" && <Suspense fallback={<PanelSkeleton kind="artifacts" />}><ScrollablePanel><LazyArtifactsTab /></ScrollablePanel></Suspense>}
          {panelTab === "inspector" && <Suspense fallback={<PanelSkeleton kind="inspector" />}><ScrollablePanel><LazyInspectorTab /></ScrollablePanel></Suspense>}
          {panelTab === "diagnostics" && debugAllowed && <Suspense fallback={<PanelSkeleton kind="diagnostics" />}><ScrollablePanel><LazyDiagnosticsTab /></ScrollablePanel></Suspense>}
        </div>
      </div>)}
      {automationEditors.filter(editor => mountedAutomationEditors.current.has(editor.id)).map(editor => {
        const panelTab = `automation:${editor.id}`;
        const active = editor.open && editor.owner.id === conversationId && activeTab === panelTab;
        return <div key={panelTab} className="mc-sidebar-panel-view flex-1 overflow-hidden text-sm flex flex-col"
          id={`right-panel-${panelTab}`} role="tabpanel" aria-labelledby={`right-tab-${panelTab}`} hidden={!active}
          style={{ display: active ? "flex" : "none" }}>
          <Suspense fallback={<PanelSkeleton kind="inspector" />}>
            <LazySchedulerTab editor={editor} active={visible && rightPanelOpen && active} onClose={() => closeOpenTab(panelTab as StackTab)} />
          </Suspense>
        </div>;
      })}
      </div>
    </aside>
  );
};

// ── Styles ─────────────────────────────────────────────────────

const resizeHandleStyle: React.CSSProperties = {
  position: "absolute",
  top: 0,
  bottom: 0,
  // The native browser is composited above the renderer and can drift left
  // under host zoom. Keep the hit area in the main stage so every vertical
  // point remains draggable even when the view overlaps the sidebar edge.
  left: -20,
  width: 12,
  cursor: "col-resize",
  zIndex: "var(--z-composer)",
  background: "transparent",
  touchAction: "none",
};
