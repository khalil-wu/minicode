import { useI18n } from "../i18n";
import { Clock3, Code2, GitBranch, House, MoreHorizontal, Puzzle, Settings } from "lucide-react";
import { useState } from "react";
import { useAppStore } from "../stores";
import { openAutomations } from "../lib/automations-navigation";
import { ContextMenu } from "../components/ContextMenu";

export function NavigationRail() {
  const { t: tr } = useI18n();
  const settingsOpen = useAppStore((state) => state.settingsOpen);
  const settingsTab = useAppStore((state) => state.settingsTab);
  const marketplaceOpen = useAppStore((state) => state.skillsMarketplaceOpen);
  const appMode = useAppStore((state) => state.appMode);
  const [moreMenu, setMoreMenu] = useState<{ x: number; y: number } | null>(null);
  const [settingsMenu, setSettingsMenu] = useState<{ x: number; y: number } | null>(null);
  const showWorkspace = () => {
    const state = useAppStore.getState();
    if (state.settingsOpen) state.toggleSettings();
    useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app" });
    if (!state.leftSidebarWidth) state.setLeftSidebarWidth(state.leftSidebarExpandedWidth);
  };
  const showChat = () => {
    showWorkspace();
    useAppStore.getState().setAppMode("cowork");
  };
  const showCode = () => {
    showWorkspace();
    useAppStore.getState().setAppMode("code");
    const state = useAppStore.getState();
    state.focusPanel(state.panelSlots.find((slot) => slot.kind === "editor")!.id);
  };
  const showSettings = (tab: "workspaceGit" | "general") => {
    const state = useAppStore.getState();
    state.setSettingsTab(tab);
    if (!state.settingsOpen) state.toggleSettings();
  };
  return <nav className="mc-navigation-rail" aria-label={tr("应用导航")}>
    <div className="mc-navigation-rail-main">
      <button type="button" title={tr("聊天首页")} aria-label={tr("聊天首页")} aria-current={!settingsOpen && !marketplaceOpen && appMode !== "code" ? "page" : undefined} onClick={showChat}><House /></button>
      <button type="button" title="Code" aria-label="Code" aria-current={!settingsOpen && !marketplaceOpen && appMode === "code" ? "page" : undefined} onClick={showCode}><Code2 /></button>
      <button type="button" title={tr("已安排")} aria-label={tr("已安排")} aria-current={settingsOpen && settingsTab === "scheduler" ? "page" : undefined} onClick={openAutomations}><Clock3 /></button>
      <button type="button" title={tr("插件与技能")} aria-label={tr("插件与技能")} aria-current={marketplaceOpen ? "page" : undefined} onClick={() => {
        const state = useAppStore.getState();
        if (state.settingsOpen) state.toggleSettings();
        if (!state.skillsMarketplaceOpen) state.toggleSkillsMarketplace("app", "plugins");
      }}><Puzzle /></button>
      <div className="mc-navigation-rail-more">
        <button type="button" title={tr("更多工作区视图")} aria-label={tr("更多工作区视图")} aria-expanded={Boolean(moreMenu)} onClick={(event) => { const bounds = event.currentTarget.getBoundingClientRect(); setMoreMenu(moreMenu ? null : { x: bounds.right + 4, y: bounds.top }); }}><MoreHorizontal /></button>
        {moreMenu && <ContextMenu position={moreMenu} onClose={() => setMoreMenu(null)} items={[
          { label: tr("浏览器"), onClick: () => { showWorkspace(); useAppStore.getState().setRightStackTab("browser"); } },
          { label: tr("子智能体"), onClick: () => { showWorkspace(); useAppStore.getState().setRightStackTab("subagents"); } },
          { label: tr("文件与资源"), onClick: () => { showWorkspace(); useAppStore.getState().setRightStackTab("artifacts"); } },
        ]} />}
      </div>
      <span className="mc-rail-divider" />
      <button type="button" title={tr("Git 与工作树")} aria-label={tr("Git 与工作树")} onClick={() => { showWorkspace(); useAppStore.getState().openBottomTab("git"); }}><GitBranch /></button>
    </div>
    <button type="button" title={tr("设置与帮助")} aria-label={tr("设置与帮助")} className="mc-navigation-rail-settings" aria-haspopup="menu" aria-expanded={Boolean(settingsMenu)} aria-current={settingsOpen && settingsTab !== "workspaceGit" && settingsTab !== "scheduler" ? "page" : undefined} onClick={event => {
      const bounds = event.currentTarget.getBoundingClientRect(); setMoreMenu(null);
      setSettingsMenu(settingsMenu ? null : {x:bounds.right+4,y:bounds.bottom-120});
    }}><Settings /></button>
    {settingsMenu && <ContextMenu position={settingsMenu} onClose={()=>setSettingsMenu(null)} items={[
      {label:tr("设置"),icon:<Settings size={15} />,onClick:()=>showSettings("general")},
      {label:tr("帮助"),onClick:()=>{const state=useAppStore.getState();state.setSettingsTab("shortcuts");if(!state.settingsOpen)state.toggleSettings();}},
    ]} />}
  </nav>;
}
