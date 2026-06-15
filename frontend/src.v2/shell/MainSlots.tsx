import { lazy, Suspense } from "react";
import type React from "react";
import { useAppStore } from "../stores";
import type { PanelSlot } from "../stores/types";
import { ChatPane } from "../chat/ChatPane";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { ChatErrorFallback } from "../components/ChatErrorFallback";

const loadEditorPanel = () => import("../panels/EditorPanel").then((module) => ({ default: module.EditorPanel }));

const LazyEditorPanel = lazy(loadEditorPanel);

export const MainSlots = () => {
  const panelSlots = useAppStore((s) => s.panelSlots);

  const chatSlot = panelSlots.find((slot) => slot.kind === "chat") ?? { id: "main-chat", kind: "chat" as const, label: "Chat" };
  const activeSlot =
    panelSlots.find((slot) => slot.focused) ??
    chatSlot;

  return (
    <div
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
        <PanelContent slot={activeSlot} />
      </main>
    </div>
  );
};

const PanelContent = ({ slot }: { slot: PanelSlot }) => (
  <div key={slot.id} className="anim-fade-in" style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
    {slot.kind === "chat" && (
      <SafeBoundary fallback={<ChatErrorFallback />}>
        <ChatPane />
      </SafeBoundary>
    )}
    {slot.kind === "editor" && (
      <ChunkErrorBoundary>
        <Suspense fallback={<PanelSkeleton kind={slot.kind} />}>
          <SafeBoundary fallback={<PanelErrorFallback panelName="Editor" />}>
            <LazyEditorPanel />
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
