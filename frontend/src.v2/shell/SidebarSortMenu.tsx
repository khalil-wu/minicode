import { useRef, useState } from "react";
import { ContextMenu } from "../components/ContextMenu";
import { useI18n } from "../i18n";
import { SidebarSortSourceIcon } from "../lib/sidebar-sort-icons";

export const SidebarSortMenu = ({ section, mode, onSelect }: {
  section: "projects" | "chats"; mode: "manual" | "updated_at";
  onSelect: (mode: "manual" | "updated_at") => void;
}) => {
  const { t: tr } = useI18n();
  const trigger = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState<{ x: number; y: number } | null>(null);
  const label = section === "projects" ? tr("项目侧栏选项") : tr("聊天侧栏选项");
  return <>
    <button ref={trigger} type="button" className="btn-ghost mc-icon-button" aria-label={label} title={label}
      aria-haspopup="menu" aria-expanded={position !== null} onClick={event => {
        const bounds = event.currentTarget.getBoundingClientRect();
        setPosition(position ? null : { x: bounds.left, y: bounds.bottom + 4 });
      }}><SidebarSortSourceIcon kind="options" /></button>
    {position && <ContextMenu position={position} onClose={() => setPosition(null)} returnFocusTo={trigger.current!} ariaLabel={label}
      items={[{ label: tr("排序方式"), icon: <SidebarSortSourceIcon kind="sort" />, submenu: [
        { label: tr("最近活动"), checked: mode === "updated_at", onClick: () => onSelect("updated_at") },
        { label: tr("手动"), checked: mode === "manual", onClick: () => onSelect("manual") },
      ] }]} />}
  </>;
};
