import { lazy, Suspense, useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { WorkbenchShell } from "./shell/WorkbenchShell";
import { QuickOpen } from "./overlays/QuickOpen";
import { ToastContainer } from "./overlays/ToastContainer";
import { useWebSocketConnection } from "./hooks/useWebSocket";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { useDesktopEvents } from "./hooks/useDesktopEvents";
import { useWorkspaceGit } from "./hooks/useWorkspaceGit";
import { useAppStore } from "./stores";
import { ChunkErrorBoundary, SafeBoundary } from "./shell/ChunkErrorBoundary";
import { capabilityFeatureEnabled } from "./protocol/capabilities";

const CommandPalette = lazy(() => import("./overlays/CommandPalette").then((m) => ({ default: m.CommandPalette })));
const SettingsCenter = lazy(() => import("./overlays/SettingsCenter").then((m) => ({ default: m.SettingsCenter })));
const KeyboardShortcutsHelp = lazy(() => import("./overlays/KeyboardShortcutsHelp").then((m) => ({ default: m.KeyboardShortcutsHelp })));
const LiveArtifacts = lazy(() => import("./overlays/LiveArtifacts").then((m) => ({ default: m.LiveArtifacts })));
const AgentEditor = lazy(() => import("./overlays/AgentEditor").then((m) => ({ default: m.AgentEditor })));

const RouteLoading = ({ settings = false }: { settings?: boolean }) => (
  <div className="app-route-loading" data-scope={settings ? "settings" : "overlay"} role="status" aria-label={settings ? "正在加载设置" : "正在加载页面"}>
    <LoaderCircle className="animate-spin" aria-hidden="true" />
  </div>
);

export const App = () => {
  useWebSocketConnection();
  useKeyboardShortcuts();
  useDesktopEvents();
  useWorkspaceGit();

  const commandPaletteOpen = useAppStore((s) => s.commandPaletteOpen);
  const quickOpenVisible = useAppStore((s) => s.quickOpenVisible);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const shortcutsHelpOpen = useAppStore((s) => s.shortcutsHelpOpen);
  const liveArtifactsOpen = useAppStore((s) => s.liveArtifactsOpen);
  const agentEditorOpen = useAppStore((s) => s.agentEditorOpen);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const agentEditorEnabled = capabilityFeatureEnabled(runtimeCapabilities, "agent_editor", true);
  const [settingsVisited, setSettingsVisited] = useState(settingsOpen);
  const [agentEditorVisited, setAgentEditorVisited] = useState(agentEditorOpen && agentEditorEnabled);
  useEffect(() => { if (settingsOpen) setSettingsVisited(true); }, [settingsOpen]);
  useEffect(() => { if (agentEditorOpen && agentEditorEnabled) setAgentEditorVisited(true); }, [agentEditorOpen, agentEditorEnabled]);
  const overlayRoute = [
    commandPaletteOpen, settingsOpen, shortcutsHelpOpen,
    liveArtifactsOpen, agentEditorOpen && agentEditorEnabled,
  ].join(":");

  return (
    <>
      <SafeBoundary fallback={<div style={{padding: 32, textAlign: 'center'}}>Something went wrong. <button onClick={() => window.location.reload()}>Reload</button></div>}>
        <WorkbenchShell />
      </SafeBoundary>
      <ChunkErrorBoundary key={overlayRoute}>
        <Suspense fallback={<RouteLoading />}>
          {commandPaletteOpen && <CommandPalette />}
          {shortcutsHelpOpen && <KeyboardShortcutsHelp />}
          {liveArtifactsOpen && <LiveArtifacts />}
        </Suspense>
      </ChunkErrorBoundary>
      <div hidden={!settingsOpen} style={{ display: settingsOpen ? "contents" : "none" }}>
        <ChunkErrorBoundary resetKey={settingsOpen}>
          <Suspense fallback={<RouteLoading settings />}>
            {(settingsVisited || settingsOpen) && <SettingsCenter />}
          </Suspense>
        </ChunkErrorBoundary>
      </div>
      <div hidden={!agentEditorOpen || !agentEditorEnabled} style={{ display: agentEditorOpen && agentEditorEnabled ? "contents" : "none" }}>
        <ChunkErrorBoundary resetKey={agentEditorOpen}>
          <Suspense fallback={<RouteLoading />}>
            {agentEditorEnabled && (agentEditorVisited || agentEditorOpen) && <AgentEditor />}
          </Suspense>
        </ChunkErrorBoundary>
      </div>
      <QuickOpen />
      <ToastContainer placement={commandPaletteOpen || quickOpenVisible ? "bottom" : "top"} />
    </>
  );
};
