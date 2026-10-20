import { SkillDetailsDialog, type SettingsSkill } from "./SkillDetailsDialog";
import { isDesktop, revealRegisteredSkill } from "../desktop/runtime";
import { openRegisteredSkillPreview } from "../chat/openAttachmentPreview";
import { selectSkillForComposer } from "../lib/select-skill-for-composer";
import { useAppStore } from "../stores";
import { pushToast } from "./ToastContainer";

export function InstalledSkillDetailsDialog({ skill, workspaceRoot, pending, readOnly = skill.plugin_enabled === false, onToggle, onClose }: {
  skill: SettingsSkill; workspaceRoot: string; pending: boolean; readOnly?: boolean;
  onToggle: () => void; onClose: () => void;
}) {
  return <SkillDetailsDialog skill={skill} workspaceRoot={workspaceRoot} pending={pending} readOnly={readOnly}
    canToggle={skill.source_level === "user" || skill.source_level === "plugin"}
    canOpen={!window.__MINICODE_RUNTIME__?.remoteHost} canReveal={isDesktop() && !window.__MINICODE_RUNTIME__?.remoteHost}
    onToggle={onToggle} onClose={onClose}
    onOpen={() => {
      if (openRegisteredSkillPreview({ path: skill.path, workspaceRoot, conversationId: useAppStore.getState().conversationId })) {
        onClose();
        useAppStore.setState({ settingsOpen: false, skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
      }
    }}
    onReveal={() => { void revealRegisteredSkill({ path: skill.path, workspaceRoot })?.catch(error => pushToast(error instanceof Error ? error.message : String(error), "error")); }}
    onTry={() => { onClose(); selectSkillForComposer(skill, { prefillDefaultPrompt: true }); }} />;
}
