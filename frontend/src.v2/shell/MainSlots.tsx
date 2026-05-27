import { lazy, Suspense } from "react";
import type React from "react";
import {
  List,
  PanelRight,
  PlaySquare,
} from "lucide-react";
import { useAppStore } from "../stores";
import type { PanelSlot } from "../stores/types";
import { ChatPane } from "../chat/ChatPane";
import { PanelSkeleton } from "./PanelSkeleton";
import { ChunkErrorBoundary } from "./ChunkErrorBoundary";

const loadEditorPanel = () => import("../panels/EditorPanel").then((module) => ({ default: module.EditorPanel }));

const LazyEditorPanel = lazy(loadEditorPanel);

export const MainSlots = () => {
  const panelSlots = useAppStore((s) => s.panelSlots);
  const rightStackTab = useAppStore((s) => s.rightStackTab);
  const rightPanelOpen = useAppStore((s) => s.rightPanelOpen);
  const setRightStackTab = useAppStore((s) => s.setRightStackTab);
  const toggleRightPanel = useAppStore((s) => s.toggleRightPanel);

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
      <div style={codeTopBarStyle}>
        <span style={{ flex: 1 }} />
        <CodePanelButton label="Preview" active={rightPanelOpen && rightStackTab === "preview"} onClick={() => setRightStackTab("preview")}>
          <PlaySquare size={15} />
        </CodePanelButton>
        <CodePanelButton label="Activity" active={rightPanelOpen && rightStackTab === "tasks"} onClick={() => setRightStackTab("tasks")}>
          <List size={15} />
        </CodePanelButton>
        <CodePanelButton label={rightPanelOpen ? "Close side panel" : "Open side panel"} active={rightPanelOpen} onClick={toggleRightPanel}>
          <PanelRight size={15} />
        </CodePanelButton>
      </div>
      <main style={mainCanvasStyle}>
        <PanelContent slot={activeSlot} />
      </main>
    </div>
  );
};

const PanelContent = ({ slot }: { slot: PanelSlot }) => (
  <>
    {slot.kind === "chat" && <ChatPane />}
    {slot.kind === "editor" && (
      <ChunkErrorBoundary>
        <Suspense fallback={<PanelSkeleton kind={slot.kind} />}>
          <LazyEditorPanel />
        </Suspense>
      </ChunkErrorBoundary>
    )}
  </>
);

const codeTopBarStyle: React.CSSProperties = {
  height: 42,
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "0 16px",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  flexShrink: 0,
};

const CodePanelButton = ({
  active,
  children,
  label,
  onClick,
}: {
  active?: boolean;
  children: React.ReactNode;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    title={label}
    aria-label={label}
    onClick={onClick}
    style={{
      width: 30,
      height: 30,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      border: "1px solid transparent",
      borderRadius: "var(--radius-sm, 7px)",
      background: active ? "var(--surface-page)" : "transparent",
      color: active ? "var(--text-primary)" : "var(--text-muted)",
      cursor: "pointer",
      padding: 0,
    }}
  >
    {children}
  </button>
);

const mainCanvasStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
  borderTop: "1px solid color-mix(in oklch, var(--border-subtle) 35%, transparent)",
};
