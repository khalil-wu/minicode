import { SkillDetailsDialog, type SettingsSkill } from "./SkillDetailsDialog";
import { isDesktop, revealRegisteredSkill } from "../desktop/runtime";
import { openRegisteredSkillPreview } from "../chat/openAttachmentPreview";
import { selectSkillForComposer } from "../lib/select-skill-for-composer";
import { useAppStore } from "../stores";
import { pushToast } from "./ToastContainer";
import { useEffect, useRef } from "react";
import { workspaceRootsEqual } from "../lib/workspace-path";

export function InstalledSkillDetailsDialog({ skill, workspaceRoot, pending, readOnly = skill.plugin_enabled === false, onToggle, onUninstall, onClose }: {
  skill: SettingsSkill; workspaceRoot: string; pending: boolean; readOnly?: boolean;
  onToggle: () => void; onUninstall?: () => Promise<boolean>; onClose: () => void;
}) {
  const selected = useRef({ path: skill.path, workspaceRoot });
  if (selected.current.path !== skill.path || !workspaceRootsEqual(selected.current.workspaceRoot, workspaceRoot)) {
    selected.current = { path: skill.path, workspaceRoot };
  }
  const owner = selected.current;
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  return <SkillDetailsDialog skill={skill} workspaceRoot={workspaceRoot} pending={pending} readOnly={readOnly}
    onUninstall={!readOnly && skill.source_level === "user" && skill.uninstall_name && onUninstall
      ? () => { void onUninstall().then(removed => { if (removed && mounted.current && selected.current === owner) onClose(); }); } : undefined}
    canOpen={!window.__MINICODE_RUNTIME__?.remoteHost} canReveal={isDesktop() && !window.__MINICODE_RUNTIME__?.remoteHost}
    onToggle={onToggle} onClose={onClose}
    onOpen={() => {
      if (openRegisteredSkillPreview({ path: skill.path, workspaceRoot, conversationId: useAppStore.getState().conversationId })) {
        onClose();
        useAppStore.setState({ settingsOpen: false, skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
      }
    }}
    onReveal={() => { void revealRegisteredSkill({ path: skill.path, workspaceRoot })?.catch(error => pushToast(error instanceof Error ? error.message : String(error), "error")); }}
    onTry={() => {
      onClose();
      const state = useAppStore.getState();
      state.startConversationDraft({ forceNew: true, workspaceRoot: workspaceRoot || undefined,
        bindWorkspace: Boolean(workspaceRoot), appMode: state.appMode });
      selectSkillForComposer(skill, { prefillDefaultPrompt: true });
    }} />;
}
