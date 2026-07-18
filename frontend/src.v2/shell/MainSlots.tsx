import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import type React from "react";
import { FileCode2, MessagesSquare } from "lucide-react";
import { useAppStore } from "../stores";
import type { PanelSlot } from "../stores/types";
import { ChatPane } from "../chat/ChatPane";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";

const loadEditorPanel = () => import("../panels/EditorPanel").then((module) => ({ default: module.EditorPanel }));

const LazyEditorPanel = lazy(loadEditorPanel);
const COMPACT_MAIN_WIDTH = 900;

const isCompactViewport = () => (
  typeof window !== "undefined" && window.innerWidth < COMPACT_MAIN_WIDTH
);

interface MainSlotsProps {
  mode?: "split" | "tabs";
}

export const MainSlots = ({ mode = "split" }: MainSlotsProps) => {
  const panelSlots = useAppStore((s) => s.panelSlots);
  const focusPanel = useAppStore((s) => s.focusPanel);
  const resizePanel = useAppStore((s) => s.resizePanel);
  const [compact, setCompact] = useState(isCompactViewport);

  const chatSlot = panelSlots.find((slot) => slot.kind === "chat") ?? { id: "main-chat", kind: "chat" as const, label: "Chat" };
  const activeSlot =
    panelSlots.find((slot) => slot.focused) ??
    chatSlot;
  const editorSlot = panelSlots.find((slot) => slot.kind === "editor") ?? null;
  const showSwitcher = Boolean(editorSlot);
  const maximizedSlot = panelSlots.find((slot) => slot.maximized) ?? null;
  const tabbed = mode === "tabs";
  const visibleSlots = useMemo(() => {
    if (maximizedSlot) return [maximizedSlot];
    if (tabbed || compact || !editorSlot) return [activeSlot];
    return [chatSlot, editorSlot];
  }, [activeSlot, chatSlot, compact, editorSlot, maximizedSlot, tabbed]);

  useEffect(() => {
    const onResize = () => setCompact(isCompactViewport());
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  return (
    <div
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
        {showSwitcher && (tabbed || compact || maximizedSlot) && (
          <WorkbenchSlotSwitcher
            chatSlot={chatSlot}
            editorSlot={editorSlot}
            activeKind={activeSlot.kind === "editor" ? "editor" : "chat"}
            onFocus={focusPanel}
          />
        )}
        <div style={slotDeckStyle}>
          {visibleSlots.map((slot, index) => (
            <div
              key={slot.id}
              style={{
                ...slotFrameStyle,
                flex: `${Math.max(slot.size ?? 1, 0.45)} 1 0`,
                borderRight: index < visibleSlots.length - 1 ? "1px solid var(--border-subtle)" : "0",
              }}
              onMouseDown={() => focusPanel(slot.id)}
            >
              <PanelContent slot={slot} />
              {index < visibleSlots.length - 1 && (
                <ResizeHandle
                  slotId={slot.id}
                  currentSize={slot.size ?? 1}
                  neighborSize={visibleSlots[index + 1]?.size ?? 1}
                  onResize={resizePanel}
                />
              )}
            </div>
          ))}
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
  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    const pointerId = event.pointerId;
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
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      cleanup();
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
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
      aria-label="Resize main panels"
      aria-valuemin={18}
      aria-valuemax={82}
      aria-valuenow={currentPercent}
      aria-valuetext={`${currentPercent}% to the left panel`}
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
    { id: chatSlot.id, kind: "chat" as const, label: "Chat", icon: <MessagesSquare size={13} /> },
    { id: editorSlot?.id ?? "", kind: "editor" as const, label: "File", icon: <FileCode2 size={13} />, title: editorSlot?.label ?? "File" },
  ].filter((option) => option.kind === "chat" || editorSlot);

  return (
    <div style={slotSwitcherBarStyle}>
      <div role="tablist" aria-label="Main work area" style={slotSwitcherTrackStyle}>
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
              style={{
                ...slotSwitcherButtonStyle,
                color: active ? "var(--text-primary)" : "var(--text-muted)",
              }}
              onClick={() => onFocus(option.id)}
              title={option.title ?? option.label}
            >
              <span style={slotHeaderIconStyle} aria-hidden="true">{option.icon}</span>
              <span className="sr-only">{option.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
};

const PanelContent = ({ slot }: { slot: PanelSlot }) => (
  <div key={slot.id} className="anim-fade-in" style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>
    {slot.kind === "chat" && (
      <SafeBoundary fallback={<ChatErrorFallback />}>
        <ChatPane />
      </SafeBoundary>
    )}
    {slot.kind === "editor" && (
      <ChunkErrorBoundary>
        <Suspense fallback={<PanelSkeleton kind={slot.kind} />}>
          <SafeBoundary fallback={<PanelErrorFallback panelName="Editor" />}>
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
  zIndex: 2,
  width: 8,
  height: "100%",
  cursor: "col-resize",
  touchAction: "none",
};

const slotSwitcherBarStyle: React.CSSProperties = {
  minHeight: 36,
  display: "flex",
  alignItems: "center",
  justifyContent: "flex-end",
  padding: "4px 12px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
};

const slotSwitcherTrackStyle: React.CSSProperties = {
  position: "relative",
  width: 64,
  height: 28,
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
  boxShadow: "0 1px 2px rgba(0,0,0,0.12)",
  transition: "transform 150ms ease",
};

const slotSwitcherButtonStyle: React.CSSProperties = {
  position: "relative",
  zIndex: 1,
  minWidth: 0,
  height: "100%",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
  border: "1px solid transparent",
  borderRadius: "4px",
  background: "transparent",
  font: "inherit",
  cursor: "pointer",
  transition: "color 120ms ease",
};

const slotHeaderIconStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  color: "var(--text-muted)",
};
