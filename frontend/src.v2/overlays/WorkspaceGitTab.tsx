import { GitPanel } from "../panels/GitPanel";

export const WorkspaceGitTab = () => (
  <div className="settings-embedded-tool" aria-label="Git 与工作树工具">
    <GitPanel />
  </div>
);
