import { useI18n } from "../i18n";
import { useEffect, useRef } from "react";
import { Check, Lightbulb, Paperclip, Target } from "../lib/icons";
import { BrandIcon } from "../components/BrandIcon";
import { useComposerPlugins } from "./useComposerPlugins";

export function ComposerAddMenu({ workspaceRoot, planMode, goalAvailable, onFiles, onRemoteFiles, onGoal, onPlan, onPlugin, onClose }: {
  workspaceRoot: string;
  planMode: boolean;
  goalAvailable: boolean;
  onFiles: () => void;
  onRemoteFiles?: () => void;
  onGoal: () => void;
  onPlan: () => void;
  onPlugin: (id: string, displayName: string) => void;
  onClose: () => void;
}) {
  const { t: tr } = useI18n();
  const { plugins, loading, error } = useComposerPlugins(true, workspaceRoot);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => { menu.current!.querySelector<HTMLButtonElement>('button[role="menuitem"]')!.focus(); }, []);
  const choose = (action: () => void) => { onClose(); action(); };
  return <div ref={menu} className="mc-dropdown-menu composer-add-menu" role="menu" aria-label={tr("添加内容")} onKeyDown={event => {
    if (event.key === "Escape" || event.key === "Tab") { event.preventDefault(); event.stopPropagation(); onClose(); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = Array.from(menu.current!.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)'));
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next].focus();
  }}>
    <div className="composer-menu-heading">{tr("添加")}</div>
    <button type="button" role="menuitem" onClick={() => choose(onFiles)}><Paperclip size={18} /><span>{tr("文件和文件夹")}</span></button>
    {onRemoteFiles && <button type="button" role="menuitem" onClick={() => choose(onRemoteFiles)}><Paperclip size={18} /><span>{tr("添加远程文件")}</span></button>}
    <button type="button" role="menuitem" disabled={!goalAvailable} onClick={() => choose(onGoal)}><Target size={18} /><span>{tr("目标")}</span><small>{tr("设置持续追求的目标")}</small></button>
    <button type="button" role="menuitem" onClick={() => choose(onPlan)}><Lightbulb size={18} /><span>{tr("计划模式")}</span><small>{planMode ? tr("关闭计划模式") : tr("开启计划模式")}</small>{planMode && <Check size={16} />}</button>
    <div className="composer-add-section"><span>{tr("插件")}</span></div>
    {loading && <div className="composer-add-status" role="status">{tr("正在读取插件…")}</div>}
    {error && <div className="composer-add-status" role="alert">{error}</div>}
    {plugins.filter(plugin => plugin.enabled && plugin.id).map(plugin => <button key={plugin.id} type="button" role="menuitem" onClick={() => choose(() => onPlugin(plugin.id, plugin.displayName || plugin.name))}>
      <BrandIcon value={plugin.name} size={18} /><span>{plugin.displayName || plugin.name}</span><small>{plugin.shortDescription || plugin.description}</small>
    </button>)}
  </div>;
}
