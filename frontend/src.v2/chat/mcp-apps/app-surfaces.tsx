import { lazy, Suspense, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { create } from "zustand";
import { useAppStore } from "../../stores";
import type { McpAppReceipt } from "../../protocol/mcp-app-types";
import { sendChatMessage } from "../sendChatMessage";
import { moveAppContainer } from "./app-container";
import { ProcessDetailReadingContext, TranscriptConversationReadingContext, TranscriptReadingContext } from "../transcriptReadingState";

const LazyMcpAppRenderer = lazy(() => import("./McpAppRenderer").then(module => ({ default: module.McpAppRenderer })));

interface AppSurface {
  app: McpAppReceipt;
  conversationId: string;
  scopeId: string;
  readOnly: boolean;
  inlineTarget: HTMLElement | null;
  panelTarget: HTMLElement | null;
  panelOpen: boolean;
  fallback?: ReactNode;
}

export const useMcpAppSurfaces = create<{ entries: AppSurface[] }>(() => ({ entries: [] }));
const controls = new Map<string, { wrapper: HTMLDivElement; parking: HTMLDivElement }>();
export const mcpAppTabId = (id: string): `mcp-app:${string}` => `mcp-app:${id}`;

function returnFrameToInline(id: string) {
  const control = controls.get(id);
  if (!control) return;
  const host = document.querySelector<HTMLElement>(`[data-mcp-app-id="${id}"]`);
  const inline = control.wrapper.querySelector<HTMLElement>(".mc-mcp-app-inline");
  if (host && inline) moveAppContainer(host, inline);
}

export function closeMcpAppPanel(id: string) {
  // Ref cleanup must move the live document before React removes its panel.
  returnFrameToInline(id);
  useMcpAppSurfaces.setState(state => ({ entries: state.entries.map(entry => entry.app.id === id
    ? { ...entry, panelOpen: false, panelTarget: null } : entry) }));
}

function openMcpAppPanel(id: string) {
  useMcpAppSurfaces.setState(state => ({ entries: state.entries.map(entry => entry.app.id === id
    ? { ...entry, panelOpen: true } : entry) }));
  useAppStore.getState().setRightStackTab(mcpAppTabId(id));
}

export function McpAppInlineSlot({ app, conversationId, readOnly = false, fallback }: {
  app: McpAppReceipt; conversationId: string; readOnly?: boolean; fallback?: ReactNode;
}) {
  const scopeId = useAppStore(state => state.conversationId) || conversationId;
  const conversationReading = useContext(TranscriptConversationReadingContext);
  const turnReading = useContext(TranscriptReadingContext);
  const detailReading = useContext(ProcessDetailReadingContext);
  const rawFallback = useMemo(() => <TranscriptConversationReadingContext.Provider value={conversationReading}>
    <TranscriptReadingContext.Provider value={turnReading}><ProcessDetailReadingContext.Provider value={detailReading}>
      {fallback}
    </ProcessDetailReadingContext.Provider></TranscriptReadingContext.Provider>
  </TranscriptConversationReadingContext.Provider>, [conversationReading, detailReading, fallback, turnReading]);
  const current = useRef<HTMLDivElement | null>(null);
  const bind = useCallback((target: HTMLDivElement | null) => {
    const previous = current.current;
    current.current = target;
    if (!target) {
      const control = controls.get(app.id);
      if (control?.wrapper.parentElement === previous) moveAppContainer(control.wrapper, control.parking);
      useMcpAppSurfaces.setState(state => ({ entries: state.entries.map(entry => entry.app.id === app.id && entry.inlineTarget === previous
        ? { ...entry, inlineTarget: null } : entry) }));
      return;
    }
    useMcpAppSurfaces.setState(state => {
      const entry = state.entries.find(item => item.app.id === app.id);
      return { entries: entry ? state.entries.map(item => item === entry ? { ...item, inlineTarget: target, readOnly, fallback: rawFallback } : item)
        : [...state.entries, { app, conversationId, scopeId, readOnly, inlineTarget: target, panelTarget: null, panelOpen: false, fallback: rawFallback }] };
    });
    const control = controls.get(app.id);
    if (control) moveAppContainer(control.wrapper, target);
  }, [app, conversationId, rawFallback, readOnly, scopeId]);
  return <div ref={bind} className="mc-mcp-app-slot" data-mcp-app-inline-slot={app.id} />;
}

export function McpAppPanelSlot({ id }: { id: string }) {
  const current = useRef<HTMLDivElement | null>(null);
  const bind = useCallback((target: HTMLDivElement | null) => {
    const previous = current.current;
    current.current = target;
    if (!target) returnFrameToInline(id);
    useMcpAppSurfaces.setState(state => ({ entries: state.entries.map(entry => entry.app.id === id
      && (target !== null || entry.panelTarget === previous) ? { ...entry, panelTarget: target } : entry) }));
  }, [id]);
  return <div ref={bind} className="mc-mcp-app-panel-slot" data-mcp-app-side-panel-frame-container="true" />;
}

function AppSurfaceRenderer({ entry }: { entry: AppSurface }) {
  const [wrapper] = useState(() => document.createElement("div"));
  const parking = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    parking.current!.appendChild(wrapper);
    controls.set(entry.app.id, { wrapper, parking: parking.current! });
    const target = useMcpAppSurfaces.getState().entries.find(item => item.app.id === entry.app.id)?.inlineTarget;
    if (target) moveAppContainer(wrapper, target);
    return () => { controls.delete(entry.app.id); wrapper.remove(); };
  }, [entry.app.id, wrapper]);
  return <>
    <div ref={parking} hidden data-mcp-app-parking={entry.app.id} />
    {createPortal(<Suspense fallback={<div className="mc-mcp-app-status">正在加载 {entry.app.title}…</div>}><LazyMcpAppRenderer
      app={entry.readOnly ? { ...entry.app, readOnly: true } : entry.app}
      conversationId={entry.conversationId}
      fallback={entry.fallback}
      fullscreenContainer={entry.panelOpen ? entry.panelTarget : null}
      onDisplayModeChange={mode => mode === "fullscreen" ? openMcpAppPanel(entry.app.id) : closeMcpAppPanel(entry.app.id)}
      onFollowUpMessage={entry.readOnly ? undefined : async text => await sendChatMessage({ displayContent: text,
        conversationId: entry.conversationId, allowWhileStreaming: true, awaitAdmission: true,
        busyBehavior: useAppStore.getState().followUpBehavior })}
    /></Suspense>, wrapper)}
  </>;
}

/** Separate from virtual rows and live/history layout; a portal's container never changes. */
export function McpAppSurfaces() {
  const scopeId = useAppStore(state => state.conversationId);
  const entries = useMcpAppSurfaces(state => state.entries);
  useEffect(() => {
    useMcpAppSurfaces.setState(state => state.entries.some(entry => entry.scopeId !== scopeId)
      ? { entries: state.entries.filter(entry => entry.scopeId === scopeId) } : state);
  }, [scopeId]);
  return <>{entries.filter(entry => entry.scopeId === scopeId).map(entry => <AppSurfaceRenderer key={entry.app.id} entry={entry} />)}</>;
}
