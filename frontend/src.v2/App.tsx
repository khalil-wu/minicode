import { lazy, Suspense } from "react";
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
const AutomationsCenter = lazy(() => import("./overlays/AutomationsCenter").then((m) => ({ default: m.AutomationsCenter })));
const KeyboardShortcutsHelp = lazy(() => import("./overlays/KeyboardShortcutsHelp").then((m) => ({ default: m.KeyboardShortcutsHelp })));
const SkillsMarketplace = lazy(() => import("./overlays/SkillsMarketplace").then((m) => ({ default: m.SkillsMarketplace })));
const LiveArtifacts = lazy(() => import("./overlays/LiveArtifacts").then((m) => ({ default: m.LiveArtifacts })));
const AgentEditor = lazy(() => import("./overlays/AgentEditor").then((m) => ({ default: m.AgentEditor })));

const RouteLoading = () => (
  <div className="app-route-loading" role="status" aria-label="正在加载页面">
    <LoaderCircle className="spin" aria-hidden="true" />
  </div>
);

export const App = () => {
  useWebSocketConnection();
  useKeyboardShortcuts();
  useDesktopEvents();
  useWorkspaceGit();

  const commandPaletteOpen = useAppStore((s) => s.commandPaletteOpen);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const automationsOpen = useAppStore((s) => s.automationsOpen);
  const shortcutsHelpOpen = useAppStore((s) => s.shortcutsHelpOpen);
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);
  const liveArtifactsOpen = useAppStore((s) => s.liveArtifactsOpen);
  const agentEditorOpen = useAppStore((s) => s.agentEditorOpen);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const agentEditorEnabled = capabilityFeatureEnabled(runtimeCapabilities, "agent_editor", true);

  return (
    <>
      <SafeBoundary fallback={<div style={{padding: 32, textAlign: 'center'}}>Something went wrong. <button onClick={() => window.location.reload()}>Reload</button></div>}>
        {!settingsOpen && <WorkbenchShell />}
      </SafeBoundary>
      <ChunkErrorBoundary>
        <Suspense fallback={<RouteLoading />}>
          {commandPaletteOpen && <CommandPalette />}
          {settingsOpen && <SettingsCenter />}
          {automationsOpen && <AutomationsCenter />}
          {shortcutsHelpOpen && <KeyboardShortcutsHelp />}
          {skillsMarketplaceOpen && <SkillsMarketplace />}
          {liveArtifactsOpen && <LiveArtifacts />}
          {agentEditorOpen && agentEditorEnabled && <AgentEditor />}
        </Suspense>
      </ChunkErrorBoundary>
      <QuickOpen />
      <ToastContainer />
    </>
  );
};
