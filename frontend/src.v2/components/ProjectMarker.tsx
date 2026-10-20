import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { DEFAULT_PROJECT_APPEARANCE, PROJECT_COLORS, PROJECT_ICONS, type ProjectAppearance } from "../lib/project-appearance";
import "./ProjectMarker.css";

export function ProjectMarkerIcon({ appearance, size = 18 }: { appearance?: ProjectAppearance | null; size?: number }) {
  const theme = useAppStore(state => state.resolvedTheme);
  const selected = appearance || DEFAULT_PROJECT_APPEARANCE;
  const Icon = selected.marker.kind === "icon" ? PROJECT_ICONS[selected.marker.icon] : null;
  return <span className="mc-project-marker-icon" style={{ color: PROJECT_COLORS[selected.color][theme === "dark" ? 1 : 0] }}>
    {Icon ? <Icon size={size} /> : selected.marker.kind === "emoji" ? selected.marker.emoji : null}
  </span>;
}

export function ProjectMarker({ name, appearance, onChange }: { name: string; appearance?: ProjectAppearance | null; onChange: (appearance: ProjectAppearance | null) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<"icons" | "emoji">("icons");
  const [query, setQuery] = useState("");
  const [emojis, setEmojis] = useState<Array<{ emoji: string; keywords: string; category: string }>>([]);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useFocusTrap(open);
  const current = appearance || DEFAULT_PROJECT_APPEARANCE;
  useEffect(() => { if (open && tab === "emoji") void import("../lib/project-emojis.json").then(data => setEmojis(data.default)); }, [open, tab]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!panel.current!.contains(event.target as Node) && !trigger.current!.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside);
    panel.current!.querySelector<HTMLInputElement>("input")!.focus();
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  const bounds = open ? trigger.current!.getBoundingClientRect() : null;
  const select = (marker: ProjectAppearance["marker"]) => { onChange({ ...current, marker }); setOpen(false); trigger.current!.focus(); };
  return <>
    <button ref={trigger} type="button" className="mc-project-icon" aria-label={t("更改 {name} 的标记", { name })} aria-haspopup="dialog" aria-expanded={open} onClick={event => { event.stopPropagation(); setOpen(!open); }}><ProjectMarkerIcon appearance={appearance} /></button>
    {open && createPortal(<div ref={panel} role="dialog" aria-label={t("项目标记")} className="mc-project-marker-picker" style={{ left: Math.max(8, Math.min(bounds!.left, window.innerWidth - 336)), top: Math.max(8, Math.min(bounds!.bottom + 6, window.innerHeight - 370)) }} onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current!.focus(); } }}>
      <div className="mc-project-marker-tabs"><button type="button" aria-pressed={tab === "icons"} onClick={() => setTab("icons")}>{t("图标")}</button><button type="button" aria-pressed={tab === "emoji"} onClick={() => setTab("emoji")}>{t("表情符号")}</button></div>
      <input aria-label={t("搜索标记")} placeholder={t("搜索标记")} value={query} onChange={event => setQuery(event.target.value)} />
      <div className="mc-project-marker-colors">{Object.keys(PROJECT_COLORS).map(color => <button type="button" key={color} aria-label={color} aria-pressed={current.color === color} style={{ background: PROJECT_COLORS[color as keyof typeof PROJECT_COLORS][0] }} onClick={() => onChange({ ...current, color: color as ProjectAppearance["color"] })} />)}</div>
      <div className="mc-project-marker-grid">{tab === "icons" ? Object.entries(PROJECT_ICONS).filter(([name]) => name.includes(query.toLowerCase())).map(([name, Icon]) => <button type="button" key={name} aria-label={name} aria-pressed={current.marker.kind === "icon" && current.marker.icon === name} onClick={() => select({ kind: "icon", icon: name as keyof typeof PROJECT_ICONS })}><Icon size={20} /></button>)
        : emojis.filter(item => `${item.emoji} ${item.keywords}`.toLowerCase().includes(query.trim().toLowerCase())).map(item => <button type="button" key={item.emoji} aria-label={item.keywords} aria-pressed={current.marker.kind === "emoji" && current.marker.emoji === item.emoji} onClick={() => select({ kind: "emoji", emoji: item.emoji })}>{item.emoji}</button>)}</div>
      {appearance && <button type="button" className="mc-project-marker-clear" onClick={() => { onChange(null); setOpen(false); }}>{t("移除标记")}</button>}
    </div>, document.body)}
  </>;
}
