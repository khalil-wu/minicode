import { GitPanel } from "../panels/GitPanel";
import { useAppStore } from "../stores";

export const WorkspaceGitTab = ({ active = true }: { active?: boolean }) => (
  <div className="settings-embedded-tool" aria-label="Git 与工作树工具">
    <GitPanel active={active} onEditorOpened={() => {
      const state = useAppStore.getState();
      if (state.settingsOpen) state.toggleSettings();
    }} />
  </div>
);
