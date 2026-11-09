import { useI18n } from "../i18n";
import { AtSign, Clock3, Folder, House, MoreHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { closeAutomations, openAutomations, useAutomationsPageState } from "../lib/automations-navigation";
import { ContextMenu } from "../components/ContextMenu";
import { openPreferredWebLink } from "../lib/open-preferred-target";
import { desktop, type DesktopPetState } from "../desktop/runtime";
import { SourcePetIcon } from "../pets/SourcePetIcon";
import { MenuSourceIcon } from "../lib/menu-source-icons";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import { pushToast } from "../overlays/ToastContainer";
import "./ShellMenus.css";

export function NavigationRail() {
  const { t: tr } = useI18n();
  const settingsOpen = useAppStore((state) => state.settingsOpen);
  const settingsTab = useAppStore((state) => state.settingsTab);
  const { open: automationsOpen } = useAutomationsPageState();
  const marketplaceOpen = useAppStore((state) => state.skillsMarketplaceOpen);
  const appMode = useAppStore((state) => state.appMode);
  const projectsOpen = useAppStore((state) => state.projectsOpen);
  const settingsShortcut = useAppStore(state => state.shortcutBindings.settings);
  const [moreMenu, setMoreMenu] = useState<{ x: number; y: number } | null>(null);
  const [settingsMenu, setSettingsMenu] = useState<{ x: number; y: number } | null>(null);
  const profileTrigger = useRef<HTMLButtonElement>(null);
  const pets = desktop()?.pets;
  const [petState, setPetState] = useState<DesktopPetState>();
  useEffect(() => {
    if (!pets) return;
    let current = true;
    let receivedState = false;
    const unsubscribe = pets.onStateChanged(state => { receivedState = true; if (current) setPetState(state); });
    void pets.getState().then(state => { if (current && !receivedState) setPetState(state); }).catch(error => { if (current) pushToast(String(error), "error"); });
    return () => { current = false; unsubscribe(); };
  }, [pets]);
  const showWorkspace = () => {
    closeAutomations();
    const state = useAppStore.getState();
    if (state.settingsOpen) state.toggleSettings();
    useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app", projectsOpen: false });
  };
  const showChat = () => {
    showWorkspace();
    useAppStore.getState().setAppMode("cowork");
  };
  const showSettings = (tab: "workspaceGit" | "general") => {
    const state = useAppStore.getState();
    state.setSettingsTab(tab);
    if (!state.settingsOpen) state.toggleSettings();
  };
  return <nav className="mc-navigation-rail" aria-label={tr("应用导航")}>
    <div className="mc-navigation-rail-main">
      <button type="button" title={tr("首页")} aria-label={tr("首页")} aria-current={!automationsOpen && !settingsOpen && !marketplaceOpen && !projectsOpen && appMode !== "code" ? "page" : undefined} onClick={showChat}><House /></button>
      <button type="button" title={tr("定时任务")} aria-label={tr("定时任务")} aria-current={!settingsOpen && automationsOpen ? "page" : undefined} onClick={openAutomations}><Clock3 /></button>
      <button type="button" title={tr("插件")} aria-label={tr("插件")} aria-current={!automationsOpen && !settingsOpen && marketplaceOpen ? "page" : undefined} onClick={() => {
        closeAutomations();
        const state = useAppStore.getState();
        useAppStore.setState({ projectsOpen: false });
        if (state.settingsOpen) state.toggleSettings();
        if (!state.skillsMarketplaceOpen) state.toggleSkillsMarketplace("app", "plugins");
        else useAppStore.setState({ skillsMarketplaceTab: "plugins", skillsMarketplaceSelectedPluginId: null, skillsMarketplaceReturnTarget: "app" });
      }}><AtSign /></button>
      <div className="mc-navigation-rail-more">
        <button type="button" title={tr("探索")} aria-label={tr("探索")} aria-current={!automationsOpen && !settingsOpen && !marketplaceOpen && projectsOpen ? "page" : undefined} aria-haspopup="menu" aria-expanded={Boolean(moreMenu)} onClick={(event) => { const bounds = event.currentTarget.getBoundingClientRect(); setSettingsMenu(null); setMoreMenu(moreMenu ? null : { x: bounds.right + 4, y: bounds.top }); }}><MoreHorizontal /></button>
        {moreMenu && <ContextMenu position={moreMenu} onClose={() => setMoreMenu(null)} items={[
          { label: tr("项目"), icon: <Folder size={15} />, onClick: () => { showWorkspace(); useAppStore.setState({ projectsOpen: true }); } },
        ]} />}
      </div>
    </div>
    <button ref={profileTrigger} type="button" title={tr("打开个人资料菜单")} aria-label={tr("打开个人资料菜单")} className="mc-navigation-rail-settings" aria-haspopup="menu" aria-expanded={Boolean(settingsMenu)} aria-current={settingsOpen && settingsTab !== "scheduler" ? "page" : undefined} onClick={event => {
      const bounds = event.currentTarget.getBoundingClientRect(); setMoreMenu(null);
      setSettingsMenu(settingsMenu ? null : { x: bounds.right + 4, y: bounds.bottom });
    }}><MenuSourceIcon kind="settings" /></button>
    {settingsMenu && <ContextMenu className="mc-profile-menu-rail" position={settingsMenu} anchor={profileTrigger.current!} returnFocusTo={profileTrigger.current!}
      side="right" align="end" alignOffset={-4} onClose={()=>setSettingsMenu(null)} items={[
      ...(pets && petState ? [{ label: tr(petState.miniOpen ? "隐藏虚拟宠物" : "显示虚拟宠物"), icon: <SourcePetIcon active={petState.miniOpen} />,
        onClick: () => { void pets.update({ petVisible: !petState.miniOpen }).catch(error => pushToast(String(error), "error")); } }] : []),
      {label:tr("设置"),icon:<MenuSourceIcon kind="settings" />,shortcut:formatShortcut(settingsShortcut),onClick:()=>showSettings("general")},
      {label:"",separator:true},
      {label:tr("帮助"),icon:<MenuSourceIcon kind="help" />,alignToParentBottom:true,submenuClassName:"mc-profile-help-menu",submenu:[
        {label:tr("新增内容"),icon:<MenuSourceIcon kind="whats-new" />,onClick:()=>{void openPreferredWebLink("https://developers.openai.com/codex/changelog");}},
        {label:"",separator:true},
        {label:tr("键盘快捷键"),icon:<MenuSourceIcon kind="shortcuts" />,onClick:()=>useAppStore.getState().toggleShortcutsHelp()},
        {label:tr("使用指南"),icon:<MenuSourceIcon kind="guide" />,onClick:()=>{void openPreferredWebLink("https://learn.chatgpt.com/docs/quickstart");}},
      ]},
    ]} />}
  </nav>;
}
