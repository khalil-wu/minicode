import { GitPreferencesSettings } from "./GitPreferencesSettings";

export const WorkspaceGitTab = ({ active = true }: { active?: boolean }) => {
  return <GitPreferencesSettings active={active} />;
};
