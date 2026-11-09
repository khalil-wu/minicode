import { useState, type ReactElement } from "react";
import { ChevronDown, ChevronRight, MoreHorizontal, Plus, SquarePen } from "lucide-react";
import { ContextMenu } from "../components/ContextMenu";
import type { SidebarSection } from "../protocol/thread-metadata";
import { useI18n } from "../i18n";
import { SidebarSortableList, type SidebarDragBinding } from "./SidebarSortableList";
import { sectionHeaderRowStyle } from "./sidebarStyles";

type Props = {
  sections: SidebarSection[];
  visibleItemKeys: (section: SidebarSection) => string[];
  renderItem: (section: SidebarSection, key: string, binding?: SidebarDragBinding) => ReactElement | null;
  collapsed: Set<string>;
  onToggle: (id: string) => void;
  onNew: () => void;
  onEdit: (section: SidebarSection) => void;
  onRemove: (id: string) => void;
  onRead: (section: SidebarSection) => void;
  onArchive: (section: SidebarSection) => void;
  onNewChat: (section: SidebarSection) => void;
  onReorder: (section: SidebarSection | null, moved: string, before: string | null, visible: string[]) => Promise<boolean>;
};

export function SidebarSections(props: Props) {
  const { t } = useI18n();
  const [menu, setMenu] = useState<{ section: SidebarSection; x: number; y: number; anchor: HTMLElement } | null>(null);
  const renderSection = (section: SidebarSection, binding?: SidebarDragBinding, overlay = false) => {
    const keys = props.visibleItemKeys(section), collapsed = props.collapsed.has(`section:${section.id}`);
    return <section aria-label={section.name} className="mc-sidebar-custom-section">
      <div className="mc-sidebar-project-heading" style={sectionHeaderRowStyle}>
        <button ref={binding?.dragActivatorRef} {...binding?.dragAttributes} type="button" className="btn-ghost mc-workspace-group-toggle"
          style={{ flex: 1, minWidth: 0, justifyContent: "flex-start" }} aria-expanded={!collapsed}
          onClick={() => props.onToggle(`section:${section.id}`)}>
          {collapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}<span className="mc-sidebar-project-label">{section.name}</span>
        </button>
        {!overlay && <div className="mc-workspace-actions">
          <button type="button" className="btn-ghost mc-icon-button" aria-label={t("分组选项：{name}", { name: section.name })}
            aria-haspopup="menu" onClick={event => { const rect = event.currentTarget.getBoundingClientRect(); setMenu({ section, x: rect.left, y: rect.bottom + 4, anchor: event.currentTarget }); }}><MoreHorizontal size={15} /></button>
          <button type="button" className="btn-ghost mc-icon-button" aria-label={t("在 {name} 中新建聊天", { name: section.name })}
            onClick={() => props.onNewChat(section)}><SquarePen size={15} /></button>
        </div>}
      </div>
      {!collapsed && !overlay && <SidebarSortableList identifiers={keys} scope="section" ariaLabel={section.name}
        onDragStart={() => setMenu(null)} renderRow={(key, itemBinding) => props.renderItem(section, key, itemBinding)}
        renderOverlay={key => props.renderItem(section, key)} onReorder={(moved, before, visible) => props.onReorder(section, moved, before, visible)} />}
    </section>;
  };
  return <>
    <SidebarSortableList scope="sections" identifiers={props.sections.map(section => section.id)} ariaLabel={t("移动到分组")}
      onDragStart={() => setMenu(null)} onReorder={(moved, before, visible) => props.onReorder(null, moved, before, visible)}
      renderRow={(id, binding) => renderSection(props.sections.find(section => section.id === id)!, binding)}
      renderOverlay={id => renderSection(props.sections.find(section => section.id === id)!, undefined, true)} />
    {menu && <ContextMenu position={menu} anchor={menu.anchor} onClose={() => setMenu(null)} items={[
      { label: t("编辑"), onClick: () => props.onEdit(menu.section) },
      { label: "", separator: true },
      { label: t("全部标记为已读"), onClick: () => props.onRead(menu.section) },
      { label: t("归档聊天"), onClick: () => props.onArchive(menu.section) },
      { label: "", separator: true },
      { label: t("新建分组"), icon: <Plus size={14} />, onClick: props.onNew },
      { label: t("移除"), onClick: () => props.onRemove(menu.section.id) },
    ]} />}
  </>;
}
