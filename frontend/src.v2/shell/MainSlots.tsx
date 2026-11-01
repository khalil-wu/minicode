import { LocalEnvironmentActions } from "./LocalEnvironmentActions";
import { useI18n } from "../i18n";
import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import { ArrowLeft, Columns2, Folder, Maximize2, Minimize2, Search, SlidersHorizontal, PanelRightOpen } from "lucide-react";
import { useAppStore } from "../stores";
import type { PanelSlot } from "../stores/types";
import { ChatPane } from "../chat/ChatPane";
import { threadSummaryDisplayMode, threadSummarySurface } from "../chat/thread-summary-layout";
import { openNewBrowserTab, returnToBrowserPage } from "../chat/openWebInBrowser";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";
import { ChatActionsButton } from "./ChatActionsButton";
import { fileWorkspaceScope } from "../protocol/file-workspace";
import { WorkspaceFileTabs } from "./WorkspaceFileTabs";

const loadEditorPanel = () => import("../panels/EditorPanel").then((module) => ({ default: module.EditorPanel }));

const LazyEditorPanel = lazy(loadEditorPanel);
const LazyFileViewerPanel = lazy(() => import("../panels/FileViewerPanel").then(module => ({ default: module.FileViewerPanel })));
const COMPACT_MAIN_WIDTH = 900;

interface MainSlotsProps {
  mode?: "split" | "tabs";
  forceChat?: boolean;
}

export const MainSlots = ({ mode = "split", forceChat: requestedForceChat = false }: MainSlotsProps) => {
  const { t: tr } = useI18n();
  const floating = useAppStore((s) => s.rightPanelExpanded && s.rightPanelOpen);
  const forceChat = requestedForceChat || floating;
  const allPanelSlots = useAppStore((s) => s.panelSlots);
  const fileTabs = useAppStore((s) => s.workspaceFileTabs);
  const fileScope = useAppStore(fileWorkspaceScope);
  const lastAuxiliarySlot = useRef<string | null>(null);
  const panelSlots = useMemo(() => allPanelSlots.filter(slot => slot.kind !== "file"
    || fileTabs.some(tab => tab.id === slot.fileTabId && (tab.owner.scopeId === fileScope
      || tab.owner.conversationId === null && (slot.focused || slot.id === lastAuxiliarySlot.current)))), [allPanelSlots, fileTabs, fileScope]);
  const appMode = useAppStore((s) => s.appMode);
  const focusPanel = useAppStore((s) => s.focusPanel);
  const togglePanelMaximized = useAppStore((s) => s.togglePanelMaximized);
  const resizePanel = useAppStore((s) => s.resizePanel);
  const setWorkbenchLayout = useAppStore((s) => s.setWorkbenchLayout);
  const hasOpenEditor = useAppStore((s) => s.editorTabs.length > 0);
  const conversationId = useAppStore((s) => s.conversationId);
  const pendingConversationSwitchId = useAppStore((s) => s.pendingConversationSwitchId);
  const conversationTitle = useAppStore((s) => s.conversations.find((item) => item.id === (s.pendingConversationSwitchId || s.conversationId))?.title);
  const previewReturnTarget = useAppStore((s) => s.diffReview?.conversationId === s.conversationId
    && s.diffReview?.previewReturnTarget?.conversationId === s.conversationId ? s.diffReview.previewReturnTarget : undefined);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const contextCardCollapsed = useAppStore((s) => s.contextCardCollapsed);
  const contextCardPinned = useAppStore((s) => s.contextCardPinned);
  const toggleContextCard = useAppStore((s) => s.toggleContextCard);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(false);
  const [summaryDisplayMode, setSummaryDisplayMode] = useState(() => threadSummaryDisplayMode(window.innerWidth));

  const chatSlot = panelSlots.find((slot) => slot.kind === "chat") ?? { id: "main-chat", kind: "chat" as const, label: tr("对话") };
  const focusedSlot = panelSlots.find((slot) => slot.focused) ?? chatSlot;
  const editorSlot = panelSlots.find((slot) => slot.kind === "editor") ?? null;
  const fileSlots = panelSlots.filter(slot => slot.kind === "file");
  if (focusedSlot.kind === "file" || focusedSlot.kind === "editor") lastAuxiliarySlot.current = focusedSlot.id;
  const auxiliarySlot = panelSlots.find(slot => slot.id === lastAuxiliarySlot.current) ?? editorSlot ?? fileSlots[0];
  const showEditorLayout = Boolean(auxiliarySlot) && !forceChat;
  const home = !conversationId && !pendingConversationSwitchId && !showEditorLayout && !floating;
  const maximizedSlot = panelSlots.find((slot) => slot.maximized) ?? null;
  const effectiveMaximizedSlot = forceChat ? null : maximizedSlot;
  const tabbed = mode === "tabs";
  const activeSlot = forceChat ? chatSlot : effectiveMaximizedSlot
    ?? ((tabbed || compact) && auxiliarySlot ? auxiliarySlot : focusedSlot);
  const visibleSlots = useMemo(() => {
    if (forceChat) return [chatSlot];
    if (effectiveMaximizedSlot) return [effectiveMaximizedSlot];
    if (tabbed || compact || !auxiliarySlot) return [activeSlot];
    return [chatSlot, auxiliarySlot];
  }, [activeSlot, chatSlot, compact, auxiliarySlot, effectiveMaximizedSlot, forceChat, tabbed]);
  const editorVisited = useRef(false);
  if (visibleSlots.some((slot) => slot.kind === "editor")) editorVisited.current = true;
  const keepEditorMounted = hasOpenEditor || editorVisited.current;
  // Keep the chat tree mounted behind an active Code editor. Conversation
  // history, virtual-row measurements, composer state, and scroll position
  // then survive switching between the chat and Code rail entries.
  const mountedSlots = useMemo(() => {
    return [chatSlot, ...editorSlot && keepEditorMounted ? [editorSlot] : [],
      ...allPanelSlots.filter(slot => slot.kind === "file")];
  }, [chatSlot, editorSlot, keepEditorMounted, allPanelSlots]);

  useLayoutEffect(() => {
    const surface = surfaceRef.current!;
    const chatFrame = surface.querySelector<HTMLElement>('.mc-main-slot-frame[data-panel-slot-kind="chat"]')!;
    const updateWidth = () => {
      setCompact(surface.getBoundingClientRect().width < COMPACT_MAIN_WIDTH);
      setSummaryDisplayMode(threadSummaryDisplayMode(chatFrame.clientWidth));
    };
    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(surface);
    observer.observe(chatFrame);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={surfaceRef}
      className="mc-main-surface"
      style={{
        flex: 1,
        minWidth: 0,
        minHeight: 0,
        position: "relative",
        display: "flex",
        flexDirection: "column",
        background: "var(--surface-base)",
        overflow: "hidden",
      }}
    >
      <main style={mainCanvasStyle}>
        {(conversationTitle || showEditorLayout || activeSlot.kind === "chat") && (
          <div className="mc-workbench-toolbar" data-home={home || undefined}>
            {conversationTitle && <span className="mc-workbench-title" title={conversationTitle}><Folder size={17} aria-hidden="true" />{conversationTitle}</span>}
            {appMode === "code" && conversationTitle && !pendingConversationSwitchId && activeSlot.kind === "chat" && (
              <button
                type="button"
                className="btn-ghost mc-icon-button"
                aria-label={tr("搜索当前对话")}
                title={tr("搜索当前对话")}
                onClick={() => window.dispatchEvent(new Event("chat:request-search"))}
              ><Search size={16} /></button>
            )}
            {showEditorLayout && (
              <div className="mc-workbench-layout-actions" role="group" aria-label={tr("工作区布局")}>
                {activeSlot.kind === "editor" && previewReturnTarget && <button
                  type="button"
                  className="btn-ghost mc-icon-button"
                  aria-label={tr("返回预览")}
                  title={previewReturnTarget.url ? tr("返回预览 · {url}", { url: previewReturnTarget.url }) : tr("返回预览")}
                  onClick={() => {
                    if (previewReturnTarget.tab === "browser" && previewReturnTarget.targetId && previewReturnTarget.url) {
                      returnToBrowserPage({ conversationId: previewReturnTarget.conversationId, targetId: previewReturnTarget.targetId, url: previewReturnTarget.url });
                    } else setRightStackTab(previewReturnTarget.tab);
                  }}
                ><ArrowLeft size={15} /></button>}
                {!compact && <button
                  type="button"
                  className="btn-ghost mc-icon-button"
                  aria-label={tabbed || effectiveMaximizedSlot ? tr("并排显示对话与文件") : tr("切换为单页显示")}
                  title={tabbed || effectiveMaximizedSlot ? tr("并排显示对话与文件") : tr("切换为单页显示")}
                  aria-pressed={!tabbed && !effectiveMaximizedSlot}
                  onClick={() => {
                    if (effectiveMaximizedSlot) togglePanelMaximized(effectiveMaximizedSlot.id);
                    setWorkbenchLayout(tabbed || effectiveMaximizedSlot ? "split" : "tabs");
                  }}
                ><Columns2 size={15} /></button>}
                <button
                  type="button"
                  className="btn-ghost mc-icon-button"
                  aria-label={effectiveMaximizedSlot ? tr("退出专注模式") : tr("专注当前面板")}
                  title={effectiveMaximizedSlot ? tr("退出专注模式") : tr("专注当前面板")}
                  aria-pressed={Boolean(effectiveMaximizedSlot)}
                  onClick={() => togglePanelMaximized(activeSlot.id)}
                >{effectiveMaximizedSlot ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
              </div>
            )}
            <div className="mc-workbench-task-actions">
              {!home && <><LocalEnvironmentActions /><ChatActionsButton /></>}
              {!home && visibleSlots.some((slot) => slot.kind === "chat") && <button type="button" className="btn-ghost mc-icon-button" aria-label={tr("切换摘要")} title={tr("切换摘要")}
                aria-pressed={threadSummarySurface(summaryDisplayMode, contextCardPinned, !contextCardCollapsed) !== null} aria-controls="thread-summary-panel" onClick={() => {
                  const chatFrame = surfaceRef.current!.querySelector<HTMLElement>('.mc-main-slot-frame[data-panel-slot-kind="chat"]')!;
                  toggleContextCard(chatFrame.clientWidth);
                }}><SlidersHorizontal size={17} /></button>}
              {(!rightPanelOpen || (effectiveMaximizedSlot && effectiveMaximizedSlot.kind !== "chat")) && <button
                type="button" className="btn-ghost mc-icon-button" aria-label={tr("新建标签页")} title={tr("新建标签页")}
                aria-controls="workbench-right-sidebar" onClick={() => {
                  if (effectiveMaximizedSlot && effectiveMaximizedSlot.kind !== "chat") togglePanelMaximized(effectiveMaximizedSlot.id);
                  openNewBrowserTab();
                }}><PanelRightOpen size={17} /></button>}
            </div>
          </div>
        )}
        {fileSlots.length > 0 && !forceChat && <WorkspaceFileTabs pane="left" activeTabId={auxiliarySlot?.kind === "file" ? auxiliarySlot.fileTabId : undefined} />}
        <div style={slotDeckStyle}>
          {mountedSlots.map((slot) => {
            const visibleIndex = visibleSlots.findIndex((candidate) => candidate.id === slot.id);
            const visible = visibleIndex >= 0;
            const focusSlot = () => floating && slot.kind === "chat"
              ? focusPanel(slot.id, { preserveRightPanelExpanded: true })
              : focusPanel(slot.id);
            return (
              <div
                key={slot.id}
                className="mc-main-slot-frame"
                data-panel-slot-kind={slot.kind}
                style={{
                  ...slotFrameStyle,
                  display: visible ? "flex" : "none",
                  flex: visibleSlots.length === 1
                    ? "1 1 0px"
                    : `${Math.max(slot.size ?? 1, 0.45)} 1 0px`,
                  borderRight: !tabbed && visible && visibleIndex < visibleSlots.length - 1
                    ? "1px solid var(--border-subtle)"
                    : "0",
                }}
                onMouseDown={focusSlot}
                onFocusCapture={() => { if (!slot.focused) focusSlot(); }}
              >
                <PanelContent slot={slot} active={visible} />
                {!tabbed && visible && visibleIndex < visibleSlots.length - 1 && (
                  <ResizeHandle
                    slotId={slot.id}
                    currentSize={slot.size ?? 1}
                    neighborSize={visibleSlots[visibleIndex + 1]?.size ?? 1}
                    neighborId={visibleSlots[visibleIndex + 1]?.id}
                    onResize={resizePanel}
                  />
                )}
              </div>
            );
          })}
        </div>
      </main>
    </div>
  );
};

const ResizeHandle = ({
  slotId,
  currentSize,
  neighborSize,
  neighborId,
  onResize,
}: {
  slotId: string;
  currentSize: number;
  neighborSize: number;
  neighborId?: string;
  onResize: (id: string, delta: number, neighborId?: string) => void;
}) => {
  const { t: tr } = useI18n();
  const resizeCleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => resizeCleanupRef.current?.(), []);
  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    resizeCleanupRef.current?.();
    const handle = event.currentTarget;
    const pointerId = event.pointerId;
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    let lastX = event.clientX;
    let nextX = lastX;
    let frame: number | null = null;
    const flushResize = () => {
      const delta = nextX - lastX;
      lastX = nextX;
      if (delta) onResize(slotId, delta, neighborId);
    };
    const onMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      nextX = moveEvent.clientX;
      if (frame === null) frame = requestAnimationFrame(() => {
        frame = null;
        flushResize();
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
      resizeCleanupRef.current = null;
    };
    const finishResize = () => { flushResize(); cleanup(); };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      finishResize();
    };
    resizeCleanupRef.current = cleanup;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    handle.addEventListener("lostpointercapture", finishResize);
  };
  const totalSize = Math.max(currentSize + neighborSize, 0.9);
  const currentPercent = Math.round((currentSize / totalSize) * 100);
  const resizeToPercent = (percent: number) => {
    const targetSize = totalSize * (percent / 100);
    onResize(slotId, (targetSize - currentSize) * 360, neighborId);
  };
  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 10 : 3;
    let nextPercent: number | null = null;
    if (event.key === "ArrowLeft") nextPercent = currentPercent - step;
    else if (event.key === "ArrowRight") nextPercent = currentPercent + step;
    else if (event.key === "Home") nextPercent = 18;
    else if (event.key === "End") nextPercent = 82;
    else if (event.key === "Enter") nextPercent = 50;
    if (nextPercent == null) return;
    event.preventDefault();
    resizeToPercent(Math.max(18, Math.min(82, nextPercent)));
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={tr("调整主面板宽度")}
      aria-valuemin={18}
      aria-valuemax={82}
      aria-valuenow={currentPercent}
      aria-valuetext={tr("左侧面板占 {percent}%", { percent: currentPercent })}
      tabIndex={0}
      onPointerDown={startResize}
      onDoubleClick={() => resizeToPercent(50)}
      onKeyDown={handleKeyDown}
      className="mc-main-resize-handle"
      style={resizeHandleStyle}
    />
  );
};

const PanelContent = ({ slot, active }: { slot: PanelSlot; active: boolean }) => {
  const { t: tr } = useI18n();
  return (
  <div key={slot.id} className="anim-fade-in" style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
    {slot.kind === "chat" && (
      <SafeBoundary fallback={<ChatErrorFallback />}>
        <ChatPane />
      </SafeBoundary>
    )}
    {slot.kind === "editor" && (
      <ChunkErrorBoundary>
        <Suspense fallback={<PanelSkeleton kind={slot.kind} />}>
          <SafeBoundary fallback={<PanelErrorFallback panelName={tr("编辑器")} />}>
            {/* chrome="full" renders the multi-file tab strip (open many files
                like VSCode). The store already tracks multiple editorTabs; the
                strip is what lets the user see and switch between them. */}
            <LazyEditorPanel chrome="full" />
          </SafeBoundary>
        </Suspense>
      </ChunkErrorBoundary>
    )}
    {slot.kind === "file" && (
      <ChunkErrorBoundary>
        <Suspense fallback={<PanelSkeleton kind="file" />}>
          <SafeBoundary fallback={<PanelErrorFallback panelName={tr("文件")} />}>
            <LazyFileViewerPanel tabId={slot.fileTabId!} active={active} />
          </SafeBoundary>
        </Suspense>
      </ChunkErrorBoundary>
    )}
  </div>
);
};

const mainCanvasStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  minHeight: 0,
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
};

const slotDeckStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  minHeight: 0,
  display: "flex",
  overflow: "hidden",
};

const slotFrameStyle: React.CSSProperties = {
  position: "relative",
  minWidth: 0,
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
  background: "var(--surface-base)",
};

const resizeHandleStyle: React.CSSProperties = {
  position: "absolute",
  top: 0,
  right: -4,
  zIndex: "var(--z-content)",
  width: 8,
  height: "100%",
  cursor: "col-resize",
  touchAction: "none",
};
