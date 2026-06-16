import { lazy, Suspense } from "react";
import { WorkbenchShell } from "./shell/WorkbenchShell";
import { QuickOpen } from "./overlays/QuickOpen";
import { ToastContainer } from "./overlays/ToastContainer";
import { ApprovalModal } from "./overlays/ApprovalModal";  // 🔧 新增
import { AskUserPrompt } from "./overlays/AskUserPrompt";  // 🔧 新增
import { useWebSocketConnection } from "./hooks/useWebSocket";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { useDesktopEvents } from "./hooks/useDesktopEvents";
import { useWorkspaceGit } from "./hooks/useWorkspaceGit";
import { useAppStore } from "./stores";
import { ChunkErrorBoundary, SafeBoundary } from "./shell/ChunkErrorBoundary";
import { SharedBackdrop } from "./components/SharedBackdrop";

const CommandPalette = lazy(() => import("./overlays/CommandPalette").then((m) => ({ default: m.CommandPalette })));
const SettingsCenter = lazy(() => import("./overlays/SettingsCenter").then((m) => ({ default: m.SettingsCenter })));
const KeyboardShortcutsHelp = lazy(() => import("./overlays/KeyboardShortcutsHelp").then((m) => ({ default: m.KeyboardShortcutsHelp })));
const SkillsMarketplace = lazy(() => import("./overlays/SkillsMarketplace").then((m) => ({ default: m.SkillsMarketplace })));
const LiveArtifacts = lazy(() => import("./overlays/LiveArtifacts").then((m) => ({ default: m.LiveArtifacts })));

export const App = () => {
  useWebSocketConnection();
  useKeyboardShortcuts();
  useDesktopEvents();
  useWorkspaceGit();

  const commandPaletteOpen = useAppStore((s) => s.commandPaletteOpen);
  const settingsOpen = useAppStore((s) => s.settingsOpen);
  const shortcutsHelpOpen = useAppStore((s) => s.shortcutsHelpOpen);
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);
  const liveArtifactsOpen = useAppStore((s) => s.liveArtifactsOpen);

  return (
    <>
      <SafeBoundary fallback={<div style={{padding: 32, textAlign: 'center'}}>Something went wrong. <button onClick={() => window.location.reload()}>Reload</button></div>}>
        <WorkbenchShell />
      </SafeBoundary>
      <SharedBackdrop />
      <ApprovalModal />  {/* 🔧 新增：权限审批对话框 */}
      <AskUserPrompt />  {/* 🔧 新增：用户提问对话框 */}
      <ChunkErrorBoundary>
        <Suspense fallback={null}>
          {commandPaletteOpen && <CommandPalette />}
          {settingsOpen && <SettingsCenter />}
          {shortcutsHelpOpen && <KeyboardShortcutsHelp />}
          {skillsMarketplaceOpen && <SkillsMarketplace />}
          {liveArtifactsOpen && <LiveArtifacts />}
        </Suspense>
      </ChunkErrorBoundary>
      <QuickOpen />
      <ToastContainer />
    </>
  );
};
