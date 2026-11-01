import { useState } from "react";
import { X } from "../lib/icons";
import { ContextMenu, type ContextMenuItem } from "../components/ContextMenu";
import { useAppStore } from "../stores";
import type { WorkspaceFileTab } from "../stores/types";
import { fileWorkspaceScope } from "../protocol/file-workspace";
import { fileIcon } from "../lib/file-icons";
import { useI18n, type Translate } from "../i18n";
import "./WorkspaceFileTabs.css";

export function workspaceFileTabMenuItems(tab: WorkspaceFileTab, t: Translate, actions: {
  close: () => void; closeOthers: () => void; closeRight: () => void; otherCount: number; rightCount: number;
  move?: (pane: "left" | "right") => void;
}): ContextMenuItem[] {
  const move = actions.move ?? ((pane: "left" | "right") => useAppStore.getState().moveWorkspaceFileTab(tab.id, pane));
  return [
    { label: t(tab.pinned ? "取消置顶到侧边栏" : "置顶到侧边栏"), onClick: () => useAppStore.getState().pinWorkspaceFileTab(tab.id, !tab.pinned) },
    { label: "", separator: true },
    { label: t("移至左侧面板"), disabled: useAppStore.getState().appMode === "cowork", onClick: () => move("left") },
    { label: t("移至右侧面板"), onClick: () => move("right") },
    { label: "", separator: true },
    { label: t("关闭"), onClick: actions.close },
    { label: t("关闭其他标签页"), disabled: actions.otherCount === 0, onClick: actions.closeOthers },
    { label: t("关闭右侧标签页"), disabled: actions.rightCount === 0, onClick: actions.closeRight },
  ];
}

/** The left file pane uses the same content references and actions as the shared right strip. */
export function WorkspaceFileTabs({ pane, activeTabId }: { pane: "left" | "right"; activeTabId?: string }) {
  const { t } = useI18n();
  const scope = useAppStore(state => fileWorkspaceScope(state));
  const allTabs = useAppStore(state => state.workspaceFileTabs);
  const tabs = allTabs.filter(tab => tab.open && tab.pane === pane && (tab.owner.scopeId === scope || tab.id === activeTabId));
  const [menu, setMenu] = useState<{ id: string; x: number; y: number } | null>(null);
  const close = (ids: string[]) => ids.forEach(id => useAppStore.getState().closeWorkspaceFileTab(id));
  const selected = menu ? tabs.find(tab => tab.id === menu.id)! : undefined;
  const right = selected ? tabs.slice(tabs.indexOf(selected) + 1) : [];
  return <>
    <div className="mc-sidebar-right-tabs" role="tablist" aria-label={t("文件标签页")}>
      {tabs.map(tab => <div key={tab.id} className="mc-sidebar-right-tab-frame" data-active={tab.id === activeTabId} onContextMenu={event => {
        event.preventDefault(); setMenu({ id: tab.id, x: event.clientX, y: event.clientY });
      }}>
        <button type="button" role="tab" aria-selected={tab.id === activeTabId} className="mc-sidebar-right-tab" onClick={() => useAppStore.getState().activateWorkspaceFileTab(tab.id)}>
          <span className="mc-sidebar-tab-icon">{fileIcon(tab.path, { size: 16 })}</span><span className="mc-sidebar-tab-label">{tab.title}</span>
        </button><button type="button" className="mc-sidebar-right-tab-close mc-icon-button mc-icon-button-compact" aria-label={t("关闭{v0}标签页", { v0: tab.title })} onClick={() => close([tab.id])}><X size={14} /></button>
      </div>)}
    </div>
    {menu && selected && <ContextMenu position={menu} ariaLabel={t("标签页操作")} onClose={() => setMenu(null)} items={workspaceFileTabMenuItems(selected, t, {
      close: () => close([selected.id]), closeOthers: () => close(tabs.filter(tab => tab.id !== selected.id).map(tab => tab.id)),
      closeRight: () => close(right.map(tab => tab.id)), otherCount: tabs.length - 1, rightCount: right.length,
    })} />}
  </>;
}

export function PinnedWorkspaceFiles() {
  const { t } = useI18n();
  const tabs = useAppStore(state => state.workspaceFileTabs);
  const [menu, setMenu] = useState<{ id: string; x: number; y: number } | null>(null);
  return <>
    {tabs.filter(tab => tab.pinned).map(tab => <button key={tab.id} type="button" className="mc-file-viewer-pinned-row" title={tab.path}
      onClick={() => useAppStore.getState().activateWorkspaceFileTab(tab.id)} onContextMenu={event => { event.preventDefault(); setMenu({ id: tab.id, x: event.clientX, y: event.clientY }); }}>
      {fileIcon(tab.path, { size: 17 })}<span>{tab.title}</span>
    </button>)}
    {menu && <ContextMenu position={menu} onClose={() => setMenu(null)} items={[{ label: t("取消置顶到侧边栏"), onClick: () => useAppStore.getState().pinWorkspaceFileTab(menu.id, false) }]} />}
  </>;
}
