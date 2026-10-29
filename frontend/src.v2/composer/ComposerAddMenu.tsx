import { useI18n } from "../i18n";
import { useEffect, useRef, useState } from "react";
import { PromptSourceIcon } from "../lib/prompt-source-icons";
import { BrandIcon } from "../components/BrandIcon";
import { useComposerPlugins } from "./useComposerPlugins";

export function ComposerAddMenu({ workspaceRoot, planMode, planAvailable, showGoal, goalAvailable, onFiles, onRemoteFiles, onGoal, onPlan, onPlugin, onClose }: {
  workspaceRoot: string;
  planMode: boolean;
  planAvailable: boolean;
  showGoal: boolean;
  goalAvailable: boolean;
  onFiles: () => void;
  onRemoteFiles?: () => void;
  onGoal: () => void;
  onPlan: () => void;
  onPlugin: (id: string, displayName: string) => void;
  onClose: (restoreFocus: boolean) => void;
}) {
  const { t: tr } = useI18n();
  const { plugins, loading, error } = useComposerPlugins(true, workspaceRoot);
  const menu = useRef<HTMLDivElement>(null);
  const focusOwner = useRef(document.activeElement);
  const [highlighted, setHighlighted] = useState("files");
  useEffect(() => {
    const root = menu.current!;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing || event.defaultPrevented) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(true); return; }
      const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)'));
      const current = buttons.findIndex(button => button.dataset.addMenuItem === highlighted);
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault(); event.stopPropagation();
        const next = current < 0 ? event.key === "ArrowDown" ? 0 : buttons.length - 1
          : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        setHighlighted(buttons[next].dataset.addMenuItem!);
        buttons[next].scrollIntoView({ block: "nearest" });
      } else if (event.key === "Enter" && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
        event.preventDefault(); event.stopPropagation(); buttons[current < 0 ? 0 : current].click();
      }
    };
    const handleFocus = (event: FocusEvent) => {
      if (event.target !== focusOwner.current && !root.contains(event.target as Node)) onClose(false);
    };
    window.addEventListener("keydown", handleKeyDown, true);
    document.addEventListener("focusin", handleFocus);
    return () => { window.removeEventListener("keydown", handleKeyDown, true); document.removeEventListener("focusin", handleFocus); };
  }, [highlighted, onClose]);
  const choose = (action: () => void) => { onClose(true); action(); };
  return <div ref={menu} className="mc-dropdown-menu composer-add-menu" role="menu" aria-label={tr("添加内容")}
    onMouseDown={event => { if ((event.target as Element).closest('button[role="menuitem"]')) event.preventDefault(); }}>
    <div className="composer-menu-heading">{tr("添加")}</div>
    <button type="button" role="menuitem" data-add-menu-item="files" data-highlighted={highlighted === "files" || undefined} aria-current={highlighted === "files" || undefined}
      onMouseMove={() => setHighlighted("files")} onClick={() => choose(onFiles)}><PromptSourceIcon icon="paperclip" /><span>{tr("文件和文件夹")}</span></button>
    {onRemoteFiles && <button type="button" role="menuitem" data-add-menu-item="remote-files" data-highlighted={highlighted === "remote-files" || undefined} aria-current={highlighted === "remote-files" || undefined}
      onMouseMove={() => setHighlighted("remote-files")} onClick={() => choose(onRemoteFiles)}><PromptSourceIcon icon="paperclip" /><span>{tr("添加远程文件")}</span></button>}
    {showGoal && <button type="button" role="menuitem" disabled={!goalAvailable} data-add-menu-item="goal" data-highlighted={goalAvailable && highlighted === "goal" || undefined} aria-current={goalAvailable && highlighted === "goal" || undefined}
      onMouseMove={() => { if (goalAvailable) setHighlighted("goal"); }} onClick={() => choose(onGoal)}><PromptSourceIcon icon="target" /><span>{tr("目标")}</span><small>{tr("设置持续追求的目标")}</small></button>}
    <button type="button" role="menuitem" disabled={!planAvailable} data-add-menu-item="plan" data-highlighted={planAvailable && highlighted === "plan" || undefined} aria-current={planAvailable && highlighted === "plan" || undefined}
      onMouseMove={() => { if (planAvailable) setHighlighted("plan"); }} onClick={() => choose(onPlan)}><PromptSourceIcon icon="lightbulb" /><span>{tr("计划模式")}</span><small>{planMode ? tr("关闭计划模式") : tr("开启计划模式")}</small></button>
    <div className="composer-add-section"><span>{tr("插件")}</span></div>
    {loading && <div className="composer-add-status" role="status">{tr("正在读取插件…")}</div>}
    {error && <div className="composer-add-status" role="alert">{error}</div>}
    {plugins.filter(plugin => plugin.enabled && plugin.id).map(plugin => <button key={plugin.id} type="button" role="menuitem" data-add-menu-item={plugin.id}
      data-highlighted={highlighted === plugin.id || undefined} aria-current={highlighted === plugin.id || undefined}
      onMouseMove={() => setHighlighted(plugin.id)} onClick={() => choose(() => onPlugin(plugin.id, plugin.displayName || plugin.name))}>
      <BrandIcon value={plugin.name} size={16} /><span>{plugin.displayName || plugin.name}</span><small>{plugin.shortDescription || plugin.description}</small>
    </button>)}
  </div>;
}
