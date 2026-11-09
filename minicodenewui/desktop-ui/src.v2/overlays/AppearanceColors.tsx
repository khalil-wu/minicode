import { createContext, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { HexColorPicker } from "react-colorful";
import { ChevronDown } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { colorInputDraft, completeColorInput, defaultAppearanceColors, effectiveAppearanceColors, type AppearanceColorRole, type AppearanceVariant } from "../lib/appearance-colors";
import { defaultThemePreferences } from "../lib/appearance-themes";
import { desktop, isDesktop } from "../desktop/runtime";
import { pushToast } from "./ToastContainer";
import { AppearanceThemeSharing } from "./AppearanceThemeSharing";
import { AppearanceCodeThemePicker } from "./AppearanceCodeThemePicker";
import { AppearanceFonts } from "./AppearanceFonts";
import { AppearanceAccentPicker } from "./AppearanceAccentPicker";
import { isSettingsNavigationTarget } from "./settingsControlOwner";
import "./AppearanceColors.css";

const WindowCapabilities = createContext(false);
export const useAppearanceTranslucency = () => useContext(WindowCapabilities);
export function AppearanceWindowCapabilitiesProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [supportsTranslucency, setSupportsTranslucency] = useState(false);
  useEffect(() => {
    if (!isDesktop()) return;
    let current = true;
    void desktop()!.windowControls.getAppearance().then(state => { if (current) setSupportsTranslucency(state.supportsTranslucency); })
      .catch(error => { if (current) pushToast(t("窗口外观读取失败：{v0}", { v0: String(error) }), "error"); });
    const unsubscribe = desktop()!.windowControls.onAppearanceChanged(state => setSupportsTranslucency(state.supportsTranslucency));
    return () => { current = false; unsubscribe(); };
  }, [t]);
  return <WindowCapabilities.Provider value={supportsTranslucency}>{children}</WindowCapabilities.Provider>;
}

function ColorControl({ label, value, onChange, active }: { label: string; value: string; onChange: (value: string) => void; active: boolean }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const id = useId();
  const activeRef = useRef(active);
  activeRef.current = active;
  const close = (discardDraft = true) => { setOpen(false); if (discardDraft) setDraft(null); };
  useEffect(() => { if (!active) close(false); }, [active]);

  useEffect(() => {
    if (!open || !active) return;
    const outside = (event: PointerEvent) => {
      const path = event.composedPath();
      if (!path.includes(root.current!) && !path.includes(popover.current!)) close(!isSettingsNavigationTarget(event.target));
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [open, active]);

  useLayoutEffect(() => {
    if (!open || !active) return;
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
  }, [open, active]);

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
    if (open && active && !root.current!.contains(event.relatedTarget) && !popover.current!.contains(event.relatedTarget)) close(!isSettingsNavigationTarget(event.relatedTarget));
  }}>
    <button ref={trigger} type="button" className="appearance-color-swatch" style={{ backgroundColor: value }}
      aria-label={t("选择{color}", { color: label })} aria-expanded={open} aria-controls={open ? id : undefined}
      aria-haspopup="dialog" onClick={() => { setDraft(null); setOpen(!open); }} />
    <input aria-label={label} type="text" value={(draft ?? value).toUpperCase()} spellCheck={false}
      onPointerDown={event => event.stopPropagation()} onClick={event => event.stopPropagation()} onBlur={event => {
        if (activeRef.current && !isSettingsNavigationTarget(event.relatedTarget)) setDraft(null);
      }}
      onChange={event => {
        const next = colorInputDraft(event.target.value);
        const complete = completeColorInput(next);
        if (complete === null) { setDraft(next); return; }
        setDraft(null); onChange(complete);
      }} />
    {active && open && createPortal(<div ref={popover} id={id} role="dialog" aria-label={label}
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

export function AppearanceColors({ advanced = false, variant: requestedVariant, separateModes = false, active = true }: { advanced?: boolean; variant?: AppearanceVariant; separateModes?: boolean; active?: boolean } = {}) {
  const { t } = useI18n();
  const resolved = useAppStore(state => state.resolvedTheme);
  const variant = requestedVariant || resolved;
  const colors = useAppStore(state => state.appearanceColors);
  const setColor = useAppStore(state => state.setAppearanceColor);
  const selection = useAppStore(state => state.appearanceThemes[variant]);
  const setCodeThemeId = useAppStore(state => state.setCodeThemeId);
  const importTheme = useAppStore(state => state.importAppearanceTheme);
  const patchPreferences = useAppStore(state => state.patchAppearanceTheme);
  const effective = colors[variant] ?? (variant === resolved ? effectiveAppearanceColors() : defaultAppearanceColors[variant]);
  const codeThemeId = selection?.codeThemeId ?? "codex";
  const { contrast, opaqueWindows, semanticColors } = selection ?? defaultThemePreferences(variant);
  const fonts = selection?.fonts;
  const snapshot = { codeThemeId, theme: { contrast, opaqueWindows, semanticColors, fonts, ...effective, accentSource: "custom" as const } };
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const supportsTranslucency = useAppearanceTranslucency();
  const patchTheme = (patch: { contrast?: number; opaqueWindows?: boolean }) => {
    void patchPreferences(variant, patch).catch(error => {
      pushToast(t("主题更新失败：{v0}", { v0: String(error) }), "error");
    });
  };
  if (advanced) return <section className="settings-group">{separateModes && <h3 className="settings-group-title">{t("{variant}主题", { variant: variantLabel })}</h3>}<div className="settings-card">
      <AppearanceFonts advanced variant={variant} fonts={fonts} onChange={next => patchPreferences(variant, { fonts: next })} active={active} />
      {supportsTranslucency && <div className="settings-row">
        <div className="settings-row-copy"><div className="settings-row-title">{t("半透明侧栏")}</div></div>
        <div className="settings-row-control"><button type="button" className="settings-toggle" role="switch"
          aria-label={t("{variant}半透明侧栏", { variant: variantLabel })} aria-checked={!opaqueWindows}
          data-active={String(!opaqueWindows)} onClick={() => patchTheme({ opaqueWindows: !opaqueWindows })}><span /></button></div>
      </div>}
      <div className="settings-row">
        <div className="settings-row-copy"><div className="settings-row-title">{t("对比度")}</div></div>
        <div className="settings-row-control"><input type="range" min={0} max={100} step={1} value={contrast}
          aria-label={t("{variant}对比度", { variant: variantLabel })}
          onChange={event => patchTheme({ contrast: Number(event.target.value) })} /></div>
      </div>
  </div></section>;
  return <section className="settings-card settings-appearance-colors" aria-label={t("主题")}>
    <div className="settings-row appearance-theme-heading"><h4 className="settings-row-title">{separateModes ? t("{variant}主题", { variant: variantLabel }) : t("主题")}</h4><div className="appearance-theme-heading-controls">
      <AppearanceThemeSharing key={variant} variant={variant} snapshot={snapshot} onImport={importTheme} active={active} />
      <AppearanceCodeThemePicker variant={variant} colors={effective} value={codeThemeId} onSelect={id => setCodeThemeId(variant, id)} active={active} />
    </div></div>
    {roles.map(({ role, label, input }) => <div className="settings-row" key={role}>
      <div className="settings-row-copy"><div className="settings-row-title">{t(label)}</div></div>
      <div className="settings-row-control">
        {role === "accent" && <AppearanceAccentPicker variant={variant} onCustomSelect={() => setColor(variant, "accent", effective.accent)} active={active} />}
        <ColorControl key={`${variant}:${role}`} label={t(input, { variant: variantLabel })}
          value={effective[role]} onChange={value => setColor(variant, role, value)} active={active} />
      </div>
    </div>)}
    <AppearanceFonts variant={variant} fonts={fonts} onChange={next => patchPreferences(variant, { fonts: next })} active={active} />

  </section>;
}
