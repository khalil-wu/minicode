import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import { ArrowLeft, Columns2, FileCode2, Maximize2, MessagesSquare, Minimize2, Search } from "lucide-react";
import { useAppStore } from "../stores";
import type { PanelSlot } from "../stores/types";
import { ChatPane } from "../chat/ChatPane";
import { returnToBrowserPage } from "../chat/openWebInBrowser";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";

const loadEditorPanel = () => import("../panels/EditorPanel").then((module) => ({ default: module.EditorPanel }));

const LazyEditorPanel = lazy(loadEditorPanel);
const COMPACT_MAIN_WIDTH = 900;

interface MainSlotsProps {
  mode?: "split" | "tabs";
  forceChat?: boolean;
}

export const MainSlots = ({ mode = "split", forceChat = false }: MainSlotsProps) => {
  const panelSlots = useAppStore((s) => s.panelSlots);
  const focusPanel = useAppStore((s) => s.focusPanel);
  const togglePanelMaximized = useAppStore((s) => s.togglePanelMaximized);
  const resizePanel = useAppStore((s) => s.resizePanel);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const setWorkbenchLayout = useAppStore((s) => s.setWorkbenchLayout);
  const hasOpenEditor = useAppStore((s) => s.editorTabs.length > 0);
  const pendingConversationSwitchId = useAppStore((s) => s.pendingConversationSwitchId);
  const conversationTitle = useAppStore((s) => s.conversations.find((item) => item.id === (s.pendingConversationSwitchId || s.conversationId))?.title);
  const previewReturnTarget = useAppStore((s) => s.diffReview?.conversationId === s.conversationId
    && s.diffReview?.previewReturnTarget?.conversationId === s.conversationId ? s.diffReview.previewReturnTarget : undefined);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(false);

  const chatSlot = panelSlots.find((slot) => slot.kind === "chat") ?? { id: "main-chat", kind: "chat" as const, label: "对话" };
  const focusedSlot = panelSlots.find((slot) => slot.focused) ?? chatSlot;
  const activeSlot = forceChat
    ? chatSlot
    : focusedSlot;
  const editorSlot = panelSlots.find((slot) => slot.kind === "editor") ?? null;
  const showSwitcher = Boolean(editorSlot);
  const maximizedSlot = panelSlots.find((slot) => slot.maximized) ?? null;
  const effectiveMaximizedSlot = forceChat ? null : maximizedSlot;
  const tabbed = mode === "tabs";
  const visibleSlots = useMemo(() => {
    if (effectiveMaximizedSlot) return [effectiveMaximizedSlot];
    if (tabbed || compact || !editorSlot) return [activeSlot];
    return [chatSlot, editorSlot];
  }, [activeSlot, chatSlot, compact, editorSlot, effectiveMaximizedSlot, tabbed]);
  // Keep the chat tree mounted behind an active Code editor. Conversation
  // history, virtual-row measurements, composer state, and scroll position
  // then survive Cowork/Code and Chat/File switches instead of being rebuilt.
  const mountedSlots = useMemo(() => {
    if (!editorSlot) return visibleSlots;
    return hasOpenEditor || visibleSlots.some((slot) => slot.kind === "editor")
      ? [chatSlot, editorSlot]
      : [chatSlot];
  }, [chatSlot, editorSlot, hasOpenEditor, visibleSlots]);

  useLayoutEffect(() => {
    const surface = surfaceRef.current!;
    const updateWidth = () => setCompact(surface.getBoundingClientRect().width < COMPACT_MAIN_WIDTH);
    updateWidth();
    const observer = new ResizeObserver(updateWidth);
    observer.observe(surface);
    return () => observer.disconnect();
  }, []);

  const focusWorkbenchPanel = (id: string) => {
    if (forceChat && editorSlot?.id === id) setAppMode("code");
    if (effectiveMaximizedSlot && effectiveMaximizedSlot.id !== id) togglePanelMaximized(id);
    else focusPanel(id);
  };

  return (
    <div
      ref={surfaceRef}
      className="mc-main-surface"
      style={{
        flex: 1,
        minWidth: 0,
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        background: "var(--surface-base)",
        overflow: "hidden",
      }}
    >
      <main style={mainCanvasStyle}>
        {(conversationTitle || showSwitcher) && (
          <div className="mc-workbench-toolbar">
            {conversationTitle && <span className="mc-workbench-title" title={conversationTitle}>{conversationTitle}</span>}
            {conversationTitle && !pendingConversationSwitchId && activeSlot.kind === "chat" && (
              <button
                type="button"
                className="btn-ghost mc-icon-button"
                aria-label="搜索当前对话"
                title="搜索当前对话"
                onClick={() => window.dispatchEvent(new Event("chat:request-search"))}
              ><Search size={16} /></button>
            )}
            {showSwitcher && (tabbed || compact || effectiveMaximizedSlot) && (
              <WorkbenchSlotSwitcher
                chatSlot={chatSlot}
                editorSlot={editorSlot}
                activeKind={activeSlot.kind === "editor" ? "editor" : "chat"}
                onFocus={focusWorkbenchPanel}
              />
            )}
            {showSwitcher && !forceChat && (
              <div className="mc-workbench-layout-actions" role="group" aria-label="工作区布局">
                {activeSlot.kind === "editor" && previewReturnTarget && <button
                  type="button"
                  className="btn-ghost mc-icon-button"
                  aria-label="返回预览"
                  title={previewReturnTarget.url ? `返回预览 · ${previewReturnTarget.url}` : "返回预览"}
                  onClick={() => {
                    if (previewReturnTarget.tab === "browser" && previewReturnTarget.targetId && previewReturnTarget.url) {
                      returnToBrowserPage({ conversationId: previewReturnTarget.conversationId, targetId: previewReturnTarget.targetId, url: previewReturnTarget.url });
                    } else setRightStackTab(previewReturnTarget.tab);
                  }}
                ><ArrowLeft size={15} /></button>}
                {!compact && <button
                  type="button"
                  className="btn-ghost mc-icon-button"
                  aria-label={tabbed || effectiveMaximizedSlot ? "并排显示对话与文件" : "切换为单页显示"}
                  title={tabbed || effectiveMaximizedSlot ? "并排显示对话与文件" : "切换为单页显示"}
                  aria-pressed={!tabbed && !effectiveMaximizedSlot}
                  onClick={() => {
                    if (effectiveMaximizedSlot) togglePanelMaximized(effectiveMaximizedSlot.id);
                    setWorkbenchLayout(tabbed || effectiveMaximizedSlot ? "split" : "tabs");
                  }}
                ><Columns2 size={15} /></button>}
                <button
                  type="button"
                  className="btn-ghost mc-icon-button"
                  aria-label={effectiveMaximizedSlot ? "退出专注模式" : "专注当前面板"}
                  title={effectiveMaximizedSlot ? "退出专注模式" : "专注当前面板"}
                  aria-pressed={Boolean(effectiveMaximizedSlot)}
                  onClick={() => togglePanelMaximized(activeSlot.id)}
                >{effectiveMaximizedSlot ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
              </div>
            )}
          </div>
        )}
        <div style={slotDeckStyle}>
          {mountedSlots.map((slot) => {
            const visibleIndex = visibleSlots.findIndex((candidate) => candidate.id === slot.id);
            const visible = visibleIndex >= 0;
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
                onMouseDown={() => focusPanel(slot.id)}
                onFocusCapture={() => { if (!slot.focused) focusPanel(slot.id); }}
              >
                <PanelContent slot={slot} />
                {!tabbed && visible && visibleIndex < visibleSlots.length - 1 && (
                  <ResizeHandle
                    slotId={slot.id}
                    currentSize={slot.size ?? 1}
                    neighborSize={visibleSlots[visibleIndex + 1]?.size ?? 1}
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
  onResize,
}: {
  slotId: string;
  currentSize: number;
  neighborSize: number;
  onResize: (id: string, delta: number) => void;
}) => {
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
    const onMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      const delta = moveEvent.clientX - lastX;
      lastX = moveEvent.clientX;
      onResize(slotId, delta);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      handle.removeEventListener("lostpointercapture", cleanup);
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      resizeCleanupRef.current = null;
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      cleanup();
    };
    resizeCleanupRef.current = cleanup;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    handle.addEventListener("lostpointercapture", cleanup);
  };
  const totalSize = Math.max(currentSize + neighborSize, 0.9);
  const currentPercent = Math.round((currentSize / totalSize) * 100);
  const resizeToPercent = (percent: number) => {
    const targetSize = totalSize * (percent / 100);
    onResize(slotId, (targetSize - currentSize) * 360);
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
      aria-label="调整主面板宽度"
      aria-valuemin={18}
      aria-valuemax={82}
      aria-valuenow={currentPercent}
      aria-valuetext={`左侧面板占 ${currentPercent}%`}
      tabIndex={0}
      onPointerDown={startResize}
      onDoubleClick={() => resizeToPercent(50)}
      onKeyDown={handleKeyDown}
      className="mc-main-resize-handle"
      style={resizeHandleStyle}
    />
  );
};

const WorkbenchSlotSwitcher = ({
  chatSlot,
  editorSlot,
  activeKind,
  onFocus,
}: {
  chatSlot: PanelSlot;
  editorSlot: PanelSlot | null;
  activeKind: "chat" | "editor";
  onFocus: (id: string) => void;
}) => {
  const options = [
    { id: chatSlot.id, kind: "chat" as const, label: "对话", icon: <MessagesSquare size={14} /> },
    { id: editorSlot?.id ?? "", kind: "editor" as const, label: "文件", icon: <FileCode2 size={14} />, title: editorSlot?.label ?? "文件" },
  ].filter((option) => option.kind === "chat" || editorSlot);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const index = options.findIndex((option) => option.kind === activeKind);
    let nextIndex: number;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % options.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + options.length) % options.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = options.length - 1;
    else return;
    event.preventDefault();
    onFocus(options[nextIndex].id);
    event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[nextIndex].focus();
  };

  return (
    <div role="tablist" aria-label="主工作区" style={slotSwitcherTrackStyle} onKeyDown={handleKeyDown}>
      <span
        aria-hidden="true"
        style={{
          ...slotSwitcherThumbStyle,
          transform: activeKind === "editor" ? "translateX(100%)" : "translateX(0)",
        }}
      />
      {options.map((option) => {
        const active = option.kind === activeKind;
        return (
          <button
            key={option.kind}
            type="button"
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            style={{
              ...slotSwitcherButtonStyle,
              color: active ? "var(--text-primary)" : "var(--text-muted)",
            }}
            onClick={() => onFocus(option.id)}
            title={option.title ?? option.label}
          >
            <span style={slotHeaderIconStyle} aria-hidden="true">{option.icon}</span>
            <span>{option.label}</span>
          </button>
        );
      })}
    </div>
  );
};

const PanelContent = ({ slot }: { slot: PanelSlot }) => (
  <div key={slot.id} className="anim-fade-in" style={{ flex: 1, minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
    {slot.kind === "chat" && (
      <SafeBoundary fallback={<ChatErrorFallback />}>
        <ChatPane />
      </SafeBoundary>
    )}
    {slot.kind === "editor" && (
      <ChunkErrorBoundary>
        <Suspense fallback={<PanelSkeleton kind={slot.kind} />}>
          <SafeBoundary fallback={<PanelErrorFallback panelName="编辑器" />}>
            {/* chrome="full" renders the multi-file tab strip (open many files
                like VSCode). The store already tracks multiple editorTabs; the
                strip is what lets the user see and switch between them. */}
            <LazyEditorPanel chrome="full" />
          </SafeBoundary>
        </Suspense>
      </ChunkErrorBoundary>
    )}
  </div>
);

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

const slotSwitcherTrackStyle: React.CSSProperties = {
  position: "relative",
  width: 164,
  flexShrink: 0,
  height: "var(--mc-slot-switcher-height, 28px)",
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  padding: 2,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "transparent",
  overflow: "hidden",
};

const slotSwitcherThumbStyle: React.CSSProperties = {
  position: "absolute",
  left: 2,
  top: 2,
  width: "calc(50% - 2px)",
  height: "calc(100% - 4px)",
  borderRadius: "4px",
  background: "var(--surface-raised)",
  boxShadow: "var(--shadow-sm)",
  transition: "transform var(--transition-fast)",
};

const slotSwitcherButtonStyle: React.CSSProperties = {
  position: "relative",
  zIndex: "var(--z-content)",
  minWidth: 0,
  height: "100%",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 5,
  padding: "0 8px",
  border: "1px solid transparent",
  borderRadius: "4px",
  background: "transparent",
  font: "inherit",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-medium)",
  whiteSpace: "nowrap",
  cursor: "pointer",
  transition: "color var(--transition-fast)",
};

const slotHeaderIconStyle: React.CSSProperties = {
  display: "inline-flex",
  flexShrink: 0,
  alignItems: "center",
  justifyContent: "center",
  color: "var(--text-muted)",
};
