import { lazy, Suspense } from "react";
import { WorkbenchShell } from "./shell/WorkbenchShell";
import { QuickOpen } from "./overlays/QuickOpen";
import { ToastContainer } from "./overlays/ToastContainer";
import { useWebSocketConnection } from "./hooks/useWebSocket";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { useDesktopEvents } from "./hooks/useDesktopEvents";
import { useWorkspaceGit } from "./hooks/useWorkspaceGit";
import { useAppStore } from "./stores";
import { ChunkErrorBoundary } from "./shell/ChunkErrorBoundary";

const CommandPalette = lazy(() => import("./overlays/CommandPalette").then((m) => ({ default: m.CommandPalette })));
const SettingsCenter = lazy(() => import("./overlays/SettingsCenter").then((m) => ({ default: m.SettingsCenter })));
const KeyboardShortcutsHelp = lazy(() => import("./overlays/KeyboardShortcutsHelp").then((m) => ({ default: m.KeyboardShortcutsHelp })));
const SkillsMarketplace = lazy(() => import("./overlays/SkillsMarketplace").then((m) => ({ default: m.SkillsMarketplace })));

export const App = () => {
  useWebSocketConnection();
  useKeyboardShortcuts();
  useDesktopEvents();
  useWorkspaceGit();

  const commandPaletteOpen = useAppStore((s) => s.commandPaletteOpen);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const shortcutsHelpOpen = useAppStore((s) => s.shortcutsHelpOpen);
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);

  return (
    <>
      <WorkbenchShell />
      <ChunkErrorBoundary>
        <Suspense fallback={null}>
          {commandPaletteOpen && <CommandPalette />}
          {settingsOpen && <SettingsCenter />}
          {shortcutsHelpOpen && <KeyboardShortcutsHelp />}
          {skillsMarketplaceOpen && <SkillsMarketplace />}
        </Suspense>
      </ChunkErrorBoundary>
      <QuickOpen />
      <ToastContainer />
    </>
  );
};
