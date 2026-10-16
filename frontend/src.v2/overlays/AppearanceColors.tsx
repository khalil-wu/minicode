import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { HexColorPicker } from "react-colorful";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { colorInputDraft, completeColorInput, effectiveAppearanceColors, type AppearanceColorRole } from "../lib/appearance-colors";
import { defaultThemePreferences } from "../lib/appearance-themes";
import { AppearanceThemeSharing } from "./AppearanceThemeSharing";
import { AppearanceCodeThemePicker } from "./AppearanceCodeThemePicker";
import "./AppearanceColors.css";

function ColorControl({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const id = useId();
  const close = () => { setOpen(false); setDraft(null); };

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      const path = event.composedPath();
      if (!path.includes(root.current!) && !path.includes(popover.current!)) close();
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const anchor = trigger.current!.getBoundingClientRect();
      const panel = popover.current!.getBoundingClientRect();
      const below = anchor.bottom + 8;
      const top = below + panel.height <= window.innerHeight - 8 ? below : anchor.top - panel.height - 8;
      setPosition({ left: Math.max(8, Math.min(anchor.right - panel.width, window.innerWidth - panel.width - 8)), top: Math.max(8, top) });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [open]);

  const onKeyDown = (event: KeyboardEvent) => {
    if (open && event.key === "Tab" && !event.shiftKey && document.activeElement === trigger.current) {
      event.preventDefault(); event.stopPropagation();
      popover.current!.querySelector<HTMLElement>('[role="slider"]')!.focus();
      return;
    }
    if (open && event.key === "Tab" && popover.current!.contains(document.activeElement)) {
      event.preventDefault(); event.stopPropagation();
      const sliders = [...popover.current!.querySelectorAll<HTMLElement>('[role="slider"]')];
      const next = sliders.indexOf(document.activeElement as HTMLElement) + (event.shiftKey ? -1 : 1);
      if (next >= 0 && next < sliders.length) sliders[next].focus();
      else { close(); (event.shiftKey ? trigger.current! : root.current!.querySelector("input")!).focus(); }
      return;
    }
    if (event.key !== "Escape" || !open) return;
    event.preventDefault(); event.stopPropagation();
    close(); trigger.current!.focus({ preventScroll: true });
  };

  return <div ref={root} className="appearance-color-control" onKeyDown={onKeyDown} onBlur={event => {
    if (open && !root.current!.contains(event.relatedTarget) && !popover.current!.contains(event.relatedTarget)) close();
  }}>
    <button ref={trigger} type="button" className="appearance-color-swatch" style={{ backgroundColor: value }}
      aria-label={t("选择{color}", { color: label })} aria-expanded={open} aria-controls={open ? id : undefined}
      aria-haspopup="dialog" onClick={() => { setDraft(null); setOpen(!open); }} />
    <input aria-label={label} type="text" value={(draft ?? value).toUpperCase()} spellCheck={false}
      onPointerDown={event => event.stopPropagation()} onClick={event => event.stopPropagation()} onBlur={() => setDraft(null)}
      onChange={event => {
        const next = colorInputDraft(event.target.value);
        const complete = completeColorInput(next);
        if (complete === null) { setDraft(next); return; }
        setDraft(null); onChange(complete);
      }} />
    {open && createPortal(<div ref={popover} id={id} role="dialog" aria-label={label}
      className="appearance-color-popover" style={position} onKeyDown={onKeyDown}>
      <HexColorPicker color={value} onChange={color => { setDraft(null); onChange(color); }} />
    </div>, document.body)}
  </div>;
}

const roles = [
  { role: "accent", label: "强调色", input: "{variant}强调色" },
  { role: "surface", label: "背景", input: "{variant}背景颜色" },
  { role: "ink", label: "前景", input: "{variant}前景颜色" },
] as const satisfies readonly { role: AppearanceColorRole; label: string; input: string }[];

export function AppearanceColors() {
  const { t } = useI18n();
  const variant = useAppStore(state => state.resolvedTheme);
  const colors = useAppStore(state => state.appearanceColors);
  const setColor = useAppStore(state => state.setAppearanceColor);
  const selection = useAppStore(state => state.appearanceThemes[variant]);
  const setCodeThemeId = useAppStore(state => state.setCodeThemeId);
  const importTheme = useAppStore(state => state.importAppearanceTheme);
  const effective = colors[variant] ?? effectiveAppearanceColors();
  const codeThemeId = selection?.codeThemeId ?? "vscode-plus";
  const { contrast, opaqueWindows, semanticColors } = selection ?? defaultThemePreferences(variant);
  const snapshot = { codeThemeId, theme: { contrast, opaqueWindows, semanticColors, ...effective, accentSource: "custom" as const } };
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  return <section className="settings-card settings-appearance-colors" aria-label={t("主题")}>
    <div className="settings-row appearance-theme-heading"><h4 className="settings-row-title">{t("主题")}</h4><div className="appearance-theme-heading-controls">
      <AppearanceThemeSharing key={variant} variant={variant} snapshot={snapshot} onImport={importTheme} />
      <AppearanceCodeThemePicker variant={variant} colors={effective} value={codeThemeId} onSelect={id => setCodeThemeId(variant, id)} />
    </div></div>
    {roles.map(({ role, label, input }) => <div className="settings-row" key={role}>
      <div className="settings-row-copy"><div className="settings-row-title">{t(label)}</div></div>
      <div className="settings-row-control"><ColorControl key={`${variant}:${role}`} label={t(input, { variant: variantLabel })}
        value={effective[role]} onChange={value => setColor(variant, role, value)} /></div>
    </div>)}
  </section>;
}
