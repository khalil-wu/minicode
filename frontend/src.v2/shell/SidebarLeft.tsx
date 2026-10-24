import { useI18n } from "../i18n";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent, ReactNode } from "react";
import { createPortal } from "react-dom";
import { BookOpenText, Check, ChevronDown, Clock3, Puzzle, Search, SquarePen } from "../lib/icons";
import { useAppStore } from "../stores";
import { LEFT_SIDEBAR_DEFAULT_WIDTH, LEFT_SIDEBAR_MIN_WIDTH, LEFT_SIDEBAR_MAX_WIDTH, visibleLeftSidebarWidth } from "../stores/shared-helpers";
import { ConfirmDialog, type ConfirmDialogState } from "./sidebarComponents";
import { ConversationsTab } from "./ConversationsTab";
import { ContextMenu } from "../components/ContextMenu";
import { openPreferredWebLink } from "../lib/open-preferred-target";
import { closeAutomations, openAutomations } from "../lib/automations-navigation";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { useComposerPlugins } from "../composer/useComposerPlugins";
import { selectSkillForComposer } from "../lib/select-skill-for-composer";
import { MenuSourceIcon } from "../lib/menu-source-icons";
import { formatShortcut } from "../lib/keyboard-shortcuts";
import "./ShellMenus.css";

export const SidebarLeft = ({
  embedded = false,
  withGlobalRail = false,
  onNavigate,
}: {
  embedded?: boolean;
  withGlobalRail?: boolean;
  onNavigate?: () => void;
}) => {
  const { t: tr } = useI18n();
  const appMode = useAppStore((s) => s.appMode);
  const conversationId = useAppStore((s) => s.conversationId);
  const leftSidebarWidth = useAppStore((s) => s.leftSidebarWidth);
  const leftSidebarExpandedWidth = useAppStore((s) => s.leftSidebarExpandedWidth);
  const setLeftSidebarWidth = useAppStore((s) => s.setLeftSidebarWidth);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const createConversation = useAppStore((s) => s.createConversation);
  const toggleCommandPalette = useAppStore((s) => s.toggleCommandPalette);
  const toggleSkillsMarketplace = useAppStore((s) => s.toggleSkillsMarketplace);
  const skillsMarketplaceOpen = useAppStore((s) => s.skillsMarketplaceOpen);
  const toggleSettings = useAppStore((s) => s.toggleSettings);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const settingsShortcut = useAppStore(s => s.shortcutBindings.settings);
  const globalSearchEnabled = capabilityFeatureEnabled(runtimeCapabilities, "global_search", true);
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState>(null);
  const [profileMenu, setProfileMenu] = useState<{ x: number; y: number } | null>(null);
  const profileTrigger = useRef<HTMLButtonElement>(null);
  const [modeMenu, setModeMenu] = useState<{ x: number; y: number } | null>(null);
  const modeTrigger = useRef<HTMLButtonElement>(null);
  const isOpen = embedded || leftSidebarWidth > 0;
  const sidebarRef = useRef<HTMLElement>(null);
  const [shellWidth, setShellWidth] = useState(window.innerWidth);
  const visibleSidebarWidth = visibleLeftSidebarWidth(leftSidebarWidth, shellWidth);
  const resizeRef = useRef<{ x: number; width: number; nextWidth: number; didMove: boolean; frame: number | null } | null>(null);
  useEffect(() => { sidebarRef.current!.inert = !isOpen; }, [isOpen]);
  useEffect(() => {
    const resized = () => setShellWidth(window.innerWidth);
    window.addEventListener("resize", resized);
    return () => window.removeEventListener("resize", resized);
  }, []);
  useLayoutEffect(() => {
    if (embedded) return;
    const workspace = sidebarRef.current!.closest(".mc-desktop-workspace");
    if (!workspace) return;
    const observer = new ResizeObserver(([entry]) => setShellWidth(entry.contentRect.width));
    observer.observe(workspace);
    return () => observer.disconnect();
  }, [embedded]);
  useEffect(() => () => finishResize(), []);

  const finishResize = () => {
    const drag = resizeRef.current;
    if (!drag) return;
    if (drag.frame !== null) cancelAnimationFrame(drag.frame);
    if (drag.didMove) setLeftSidebarWidth(drag.nextWidth);
    resizeRef.current = null;
    document.body.classList.remove("layout-dragging");
  };
  const startResize = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    resizeRef.current = { x: event.clientX, width: visibleSidebarWidth, nextWidth: visibleSidebarWidth, didMove: false, frame: null };
    event.currentTarget.setPointerCapture(event.pointerId);
    document.body.classList.add("layout-dragging");
  };
  const resize = (event: PointerEvent<HTMLDivElement>) => {
    const drag = resizeRef.current;
    if (!drag) return;
    drag.didMove ||= event.clientX !== drag.x;
    const width = drag.width + event.clientX - drag.x;
    drag.nextWidth = width < LEFT_SIDEBAR_MIN_WIDTH / 2 ? 0 : width;
    if (drag.frame === null) drag.frame = requestAnimationFrame(() => {
      drag.frame = null;
      setLeftSidebarWidth(drag.nextWidth, { persist: false });
    });
  };
  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 40 : 10;
    const next = event.key === "ArrowLeft" ? visibleSidebarWidth - step
      : event.key === "ArrowRight" ? visibleSidebarWidth + step
      : event.key === "Home" ? LEFT_SIDEBAR_MIN_WIDTH
      : event.key === "End" ? LEFT_SIDEBAR_MAX_WIDTH
      : event.key === "Enter" ? LEFT_SIDEBAR_DEFAULT_WIDTH : null;
    if (next === null) return;
    event.preventDefault();
    setLeftSidebarWidth(next);
  };

  const startSession = () => {
    leaveMarketplace();
    createConversation({ appMode: "cowork", bindWorkspace: appMode === "code" && Boolean(workingDirectory) });
    onNavigate?.();
  };
  const navigate = (action: () => void) => {
    action();
    onNavigate?.();
  };
  const leaveMarketplace = () => {
    closeAutomations();
    useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app", projectsOpen: false });
  };
  const navigateToContent = () => {
    leaveMarketplace();
    onNavigate?.();
  };

  const openAutomationsPanel = () => navigate(openAutomations);

  const sidebarWidth = embedded ? "100%" : `${visibleSidebarWidth}px`;

  return (
    <aside
      ref={sidebarRef}
      className="mc-sidebar-left flex flex-col"
      data-open={isOpen ? "true" : "false"}
      data-embedded={embedded ? "true" : "false"}
      aria-hidden={!isOpen}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        padding: 0,
        boxSizing: "border-box",
        borderRight: embedded || !isOpen ? 0 : "1px solid color-mix(in oklch, var(--border-subtle) 55%, transparent)",
        width: sidebarWidth,
        minWidth: 0,
        background: "var(--surface-sidebar)",
        opacity: isOpen ? 1 : 0,
        pointerEvents: isOpen ? "auto" : "none",
      }}
    >
      <div className="mc-sidebar-left-inner" style={{ width: embedded ? "100%" : `${visibleSidebarWidth || leftSidebarExpandedWidth}px` }}>
      <div className="mc-sidebar-heading">
      <span className="mc-sidebar-brand">{skillsMarketplaceOpen ? tr("自定义") : <button ref={modeTrigger} type="button" className="mc-sidebar-mode-toggle" aria-label={tr("切换工作模式")} aria-haspopup="menu" aria-expanded={Boolean(modeMenu)} onClick={event => {
        const bounds = event.currentTarget.getBoundingClientRect();
        setModeMenu(modeMenu ? null : { x: bounds.left, y: bounds.bottom + 5 });
      }}>MiniCode<ChevronDown size={13} /></button>}</span>
      <div className="mc-sidebar-heading-actions">
        {globalSearchEnabled && <button type="button" className="btn-ghost mc-icon-button" aria-label={tr("搜索")} title={skillsMarketplaceOpen ? tr("搜索插件与技能") : tr("搜索任务与命令")} onClick={() => {
          if (skillsMarketplaceOpen) document.querySelector<HTMLInputElement>('.skills-workspace [role="tabpanel"]:not([hidden]) input[placeholder]')?.focus();
          else navigate(() => toggleCommandPalette());
        }}><Search size={16} /></button>}
      </div>
      </div>

      <nav hidden={skillsMarketplaceOpen} className="mc-sidebar-nav" aria-label={tr("工作区导航")} style={{ display: skillsMarketplaceOpen ? "none" : "grid", gap: 2, padding: "8px 2px 12px" }}>
        <div className="mc-sidebar-primary-actions">
          <SidebarAction icon={<SquarePen />} label={tr("新聊天")} onClick={startSession} />
        </div>
        {!withGlobalRail && <SidebarAction icon={<Clock3 />} label={tr("已安排")} onClick={openAutomationsPanel} />}
        {!withGlobalRail && <SidebarAction icon={<Puzzle />} label={tr("插件")} active={skillsMarketplaceOpen} onClick={() => navigate(() => {
          closeAutomations();
          if (skillsMarketplaceOpen) useAppStore.setState({ skillsMarketplaceTab: "plugins" });
          else toggleSkillsMarketplace("app", "plugins");
        })} />}
      </nav>

      {skillsMarketplaceOpen && <CatalogSidebar key={workingDirectory} workspaceRoot={workingDirectory} onNavigate={onNavigate} />}
      <div hidden={skillsMarketplaceOpen} className="mc-sidebar-mode-content" data-mode={appMode} style={skillsMarketplaceOpen ? { display: "none" } : undefined}>
          <ConversationsTab
            conversationId={conversationId ?? ""}
            onNavigate={navigateToContent}
            onSetConfirmDialog={(dialog) => setConfirmDialog(dialog)}
            showRecent
          />
      </div>

      {!withGlobalRail && <div className="mc-sidebar-footer">
        <button ref={profileTrigger} type="button" className="mc-sidebar-profile-button" aria-label={tr("打开个人资料菜单")} aria-haspopup="menu" aria-expanded={Boolean(profileMenu)} onClick={event => {
          const bounds = event.currentTarget.getBoundingClientRect();
          setProfileMenu(profileMenu ? null : { x: bounds.left, y: bounds.top });
        }}><MenuSourceIcon kind="settings" /><span>MiniCode</span></button>
      </div>}
      {profileMenu && <ContextMenu position={profileMenu} anchor={profileTrigger.current!} returnFocusTo={profileTrigger.current!}
        side="top" align="start" style={{ width: sidebarRef.current!.getBoundingClientRect().width - 16 }} onClose={() => setProfileMenu(null)} items={[
        { label: tr("设置"), icon: <MenuSourceIcon kind="settings" />, shortcut: formatShortcut(settingsShortcut), onClick: () => navigate(() => { useAppStore.getState().setSettingsTab("general"); toggleSettings(); }) },
        { label: "", separator: true },
        { label: tr("帮助"), icon: <MenuSourceIcon kind="help" />, alignToParentBottom: true, submenuClassName: "mc-profile-help-menu", submenu: [
          { label: tr("新增内容"), icon: <MenuSourceIcon kind="whats-new" />, onClick: () => { void openPreferredWebLink("https://developers.openai.com/codex/changelog"); } },
          { label: "", separator: true },
          { label: tr("键盘快捷键"), icon: <MenuSourceIcon kind="shortcuts" />, onClick: () => useAppStore.getState().toggleShortcutsHelp() },
          { label: tr("使用指南"), icon: <MenuSourceIcon kind="guide" />, onClick: () => { void openPreferredWebLink("https://learn.chatgpt.com/docs/quickstart"); } },
        ] },
      ]} />}
      </div>
      {modeMenu && <SidebarModeMenu position={modeMenu} mode={appMode === "code" ? "code" : "cowork"} onClose={() => setModeMenu(null)} onRestoreFocus={() => modeTrigger.current!.focus()} onSelect={mode => navigate(() => {
        leaveMarketplace(); const state = useAppStore.getState(); state.setAppMode(mode);
        if (mode === "code") state.focusPanel(useAppStore.getState().panelSlots.find(slot => slot.kind === "editor")!.id);
      })} />}
      {!embedded && <div className="mc-sidebar-left-resize-handle" role="separator" aria-orientation="vertical"
        aria-label={tr("调整左侧栏宽度")} aria-valuemin={LEFT_SIDEBAR_MIN_WIDTH} aria-valuemax={LEFT_SIDEBAR_MAX_WIDTH}
        aria-valuenow={visibleSidebarWidth} tabIndex={isOpen ? 0 : -1} title={tr("拖动调整侧栏宽度，双击恢复默认")}
        onPointerDown={startResize}
        onPointerMove={resize}
        onPointerUp={(event) => { if (resizeRef.current?.didMove) resize(event); finishResize(); }}
        onPointerCancel={finishResize} onLostPointerCapture={finishResize}
        onDoubleClick={() => setLeftSidebarWidth(LEFT_SIDEBAR_DEFAULT_WIDTH)} onKeyDown={resizeWithKeyboard} />}

      {confirmDialog && (
        <ConfirmDialog
          dialog={confirmDialog}
          onCancel={() => setConfirmDialog(null)}
          onConfirm={() => {
            const action = confirmDialog.onConfirm;
            setConfirmDialog(null);
            action();
          }}
        />
      )}
    </aside>
  );
};

function SidebarModeMenu({ position, mode, onClose, onRestoreFocus, onSelect }: {
  position: { x: number; y: number };
  mode: "cowork" | "code";
  onClose: () => void;
  onRestoreFocus: () => void;
  onSelect: (mode: "cowork" | "code") => void;
}) {
  const { t: tr } = useI18n();
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    menu.current!.querySelector<HTMLButtonElement>('[aria-current="true"]')!.focus();
    const outside = (event: MouseEvent) => { if (!menu.current!.contains(event.target as Node)) onClose(); };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [onClose]);
  return createPortal(<div ref={menu} className="mc-dropdown-menu mc-sidebar-mode-menu" role="menu" aria-label={tr("工作模式")} style={{ left: position.x, top: position.y }} onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); onClose(); onRestoreFocus(); return; }
    if (event.key === "Tab") { onClose(); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = Array.from(menu.current!.querySelectorAll<HTMLButtonElement>('button'));
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : (index + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length;
    buttons[next].focus();
  }}>
    {(["cowork", "code"] as const).map(item => <button key={item} type="button" role="menuitem" aria-label={item === "cowork" ? tr("协作") : tr("代码")} aria-current={mode === item ? "true" : undefined} onClick={() => { onClose(); onSelect(item); onRestoreFocus(); }}>
      <span><span>{item === "cowork" ? tr("协作") : tr("代码")}</span><small>{item === "cowork" ? tr("创建、学习和探索") : tr("构建、调试和发布")}</small></span>
      {mode === item && <Check size={16} aria-hidden="true" />}
    </button>)}
  </div>, document.body);
}

function CatalogSidebar({ workspaceRoot, onNavigate }: { workspaceRoot: string; onNavigate?: () => void }) {
  const { t: tr } = useI18n();
  const tab = useAppStore(state => state.skillsMarketplaceTab);
  const skills = useAppStore(state => state.availableSkills);
  const { plugins, loading, error, refresh } = useComposerPlugins(true, workspaceRoot);
  return <div className="mc-sidebar-catalog">
    <nav aria-label={tr("自定义导航")}>
      <SidebarAction icon={<Puzzle />} label={tr("插件")} active={tab === "plugins"} onClick={() => { useAppStore.setState({ skillsMarketplaceTab: "plugins" }); onNavigate?.(); }} />
      <SidebarAction icon={<BookOpenText />} label={tr("技能")} active={tab === "skills"} onClick={() => { useAppStore.setState({ skillsMarketplaceTab: "skills" }); onNavigate?.(); }} />
    </nav>
    <div className="mc-sidebar-catalog-heading">{tab === "plugins" ? tr("已安装") : tr("可用技能")}</div>
    {tab === "plugins" ? <>
      {loading && <div className="mc-sidebar-catalog-status" role="status">{tr("正在读取插件…")}</div>}
      {error && <div className="mc-sidebar-catalog-status" role="alert">{error}<button type="button" onClick={refresh}>{tr("重新读取")}</button></div>}
      {!loading && !error && !plugins.length && <div className="mc-sidebar-catalog-status">{tr("未安装任何插件")}</div>}
      {plugins.map(plugin => <SidebarAction key={plugin.id} icon={<Puzzle />} label={plugin.displayName || plugin.name} onClick={() => {
        useAppStore.setState({ skillsMarketplaceTab: "plugins", skillsMarketplaceSelectedPluginId: plugin.id }); onNavigate?.();
      }} />)}
    </> : skills.filter(skill => skill.user_invocable !== false).map(skill => <SidebarAction key={skill.path || skill.name} icon={<BookOpenText />} label={skill.display_name || skill.name} onClick={() => { selectSkillForComposer(skill); onNavigate?.(); }} />)}
  </div>;
}

const SidebarAction = ({
  icon,
  label,
  active = false,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  active?: boolean;
  onClick: () => void;
}) => (
  <button
    type="button"
    className="btn-ghost mc-sidebar-action"
    data-active={active ? "true" : undefined}
    onClick={onClick}
  >
    <span className="mc-sidebar-action-icon" aria-hidden="true">{icon}</span>
    <span>{label}</span>
  </button>
);
