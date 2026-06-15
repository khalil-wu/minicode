import { useAppStore } from "../stores";

/**
 * Shared backdrop component for all modals
 *
 * This component provides a single, unified backdrop that:
 * - Displays when any modal is open
 * - Uses consistent styling across all modals
 * - Handles click-to-close for non-critical modals
 * - Prevents multiple backdrop layers from stacking
 */
export const SharedBackdrop = () => {
  const {
    commandPaletteOpen,
    settingsOpen,
    quickOpenVisible,
    shortcutsHelpOpen,
    skillsMarketplaceOpen,
    liveArtifactsOpen,
    pendingApproval,
    toggleCommandPalette,
    toggleSettings,
    toggleQuickOpen,
    toggleShortcutsHelp,
    toggleSkillsMarketplace,
    toggleLiveArtifacts,
  } = useAppStore();

  // Check if any modal is open
  const hasModalOpen =
    commandPaletteOpen ||
    settingsOpen ||
    quickOpenVisible ||
    shortcutsHelpOpen ||
    skillsMarketplaceOpen ||
    liveArtifactsOpen ||
    !!pendingApproval;

  if (!hasModalOpen) return null;

  // Determine which modal to close on backdrop click
  // Approval modals cannot be closed by clicking backdrop
  const handleBackdropClick = () => {
    // Don't close approval modals on backdrop click
    if (pendingApproval) return;

    // Close the currently open modal
    if (commandPaletteOpen) toggleCommandPalette();
    else if (settingsOpen) toggleSettings();
    else if (quickOpenVisible) toggleQuickOpen();
    else if (shortcutsHelpOpen) toggleShortcutsHelp();
    else if (skillsMarketplaceOpen) toggleSkillsMarketplace();
    else if (liveArtifactsOpen) toggleLiveArtifacts();
  };

  return (
    <div
      className="fixed inset-0 bg-black/50"
      style={{ zIndex: "var(--z-modal-backdrop)" }}
      onClick={handleBackdropClick}
      aria-hidden="true"
    />
  );
};
