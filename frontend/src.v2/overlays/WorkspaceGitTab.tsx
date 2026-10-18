import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { GithubConnection } from "./GithubConnection";
import { GitPreferencesSettings } from "./GitPreferencesSettings";

export const WorkspaceGitTab = ({ active = true }: { active?: boolean }) => {
  const { t: tr } = useI18n();
  return (
  <>
  <GitPreferencesSettings active={active} />
  <GithubConnection active={active} />
  <section className="settings-group">
    <div className="settings-section-heading"><h3 className="settings-group-title">{tr("当前工作区")}</h3></div>
    <div className="settings-card"><div className="settings-row">
      <div className="settings-row-copy"><div className="settings-row-title">{tr("Git 与工作树")}</div>
        <div className="settings-row-description">{tr("在工作区查看变更、分支、工作树及 Pull Request。")}</div></div>
      <div className="settings-row-control"><button type="button" className="settings-action-button" onClick={() => {
        const state = useAppStore.getState();
        if (state.settingsOpen) state.toggleSettings();
        state.openBottomTab("git");
      }}>{tr("打开 Git 工作区")}</button></div>
    </div></div>
  </section>
  </>
);
};
