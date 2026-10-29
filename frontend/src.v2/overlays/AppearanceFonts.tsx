import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, LoaderCircle } from "lucide-react";
import { desktop, isDesktop } from "../desktop/runtime";
import { SelectMenu } from "../components/SelectMenu";
import { useI18n } from "../i18n";
import { pushToast } from "./ToastContainer";
import type { AppearanceVariant } from "../lib/appearance-colors";
import { cssFontFamily, firstFontFamily, type SystemFontFamily, type ThemeFonts } from "../lib/theme-fonts";
import { isSettingsNavigationTarget } from "./settingsControlOwner";

const FontCatalog = createContext<{ families: SystemFontFamily[] | null; loading: boolean }>({ families: null, loading: false });
export function AppearanceFontCatalogProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [catalog, setCatalog] = useState<{ families: SystemFontFamily[] | null; loading: boolean }>({ families: null, loading: isDesktop() });
  useEffect(() => {
    if (!isDesktop()) return;
    let current = true;
    void desktop()!.windowControls.listFontFamilies().then(families => { if (current) setCatalog({ families, loading: false }); })
      .catch(error => { if (current) { setCatalog({ families: null, loading: false }); pushToast(t("读取字体失败：{v0}", { v0: String(error) }), "error"); } });
    return () => { current = false; };
  }, [t]);
  return <FontCatalog.Provider value={catalog}>{children}</FontCatalog.Provider>;
}

function CustomFontFamily({ label, value, defaultLabel, onChange, active }: { label: string; value: string | null; defaultLabel: string; onChange: (value: string | null) => void; active: boolean }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false), [draft, setDraft] = useState(value || "");
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const trigger = useRef<HTMLButtonElement>(null), menu = useRef<HTMLDivElement>(null), input = useRef<HTMLInputElement>(null);
  const suspended = useRef(false), activeRef = useRef(active);
  activeRef.current = active;
  const close = () => { suspended.current = false; setOpen(false); trigger.current?.focus({ preventScroll: true }); };
  const commit = (next: string | null) => { onChange(next); close(); };
  const openMenu = () => {
    if (!suspended.current) setDraft(value || "");
    suspended.current = false;
    setOpen(true);
  };
  useEffect(() => {
    if (!active && open) { suspended.current = true; setOpen(false); }
  }, [active, open]);
  useEffect(() => {
    if (!open || !active) return;
    input.current!.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (!event.composedPath().includes(trigger.current!) && !event.composedPath().includes(menu.current!)) {
        suspended.current = isSettingsNavigationTarget(event.target);
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [open, active]);
  useLayoutEffect(() => {
    if (!open || !active) return;
    const place = () => {
      const anchor = trigger.current!.getBoundingClientRect(), panel = menu.current!.getBoundingClientRect();
      const below = anchor.bottom + 6;
      const top = Math.max(8, Math.min(below + panel.height > window.innerHeight - 8 ? anchor.top - panel.height - 6 : below,
        window.innerHeight - panel.height - 8));
      const left = Math.max(8, Math.min(anchor.right - panel.width, window.innerWidth - panel.width - 8));
      setPosition(previous => previous.top === top && previous.left === left ? previous : { top, left });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(trigger.current!); observer.observe(menu.current!);
    window.addEventListener("resize", place); window.addEventListener("scroll", place, true);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [open, active]);
  return <><button ref={trigger} type="button" className="appearance-custom-font-trigger" aria-label={label} aria-haspopup="menu" aria-expanded={open}
    disabled={!active} onClick={() => { if (open) close(); else openMenu(); }} onKeyDown={event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); openMenu(); }
    }}>{firstFontFamily(value) || defaultLabel}<ChevronDown size={13} /></button>
    {active && open && createPortal(<div ref={menu} className="appearance-custom-font-menu" role="menu" aria-label={label} style={position}
      onBlur={event => {
        if (activeRef.current && event.relatedTarget !== trigger.current && !event.currentTarget.contains(event.relatedTarget)) {
          suspended.current = isSettingsNavigationTarget(event.relatedTarget); setOpen(false);
        }
      }} onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
      }}>
      <button type="button" role="menuitem" onClick={() => commit(null)}>{defaultLabel}</button>
      <input ref={input} aria-label={t("{font}自定义值", { font: label })} value={draft} spellCheck={false} onChange={event => setDraft(event.target.value)}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing || event.keyCode === 229) return;
          if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); commit(draft.trim() || null); }
        }} />
      <button type="button" role="menuitem" onClick={() => commit(draft.trim() || null)}>{t("使用自定义字体值")}</button>
    </div>, document.body)}
  </>;
}

export function AppearanceFonts({ variant, fonts, onChange, advanced = false, active = true }: {
  variant: AppearanceVariant; fonts?: ThemeFonts; onChange: (fonts: ThemeFonts) => Promise<void>; advanced?: boolean; active?: boolean;
}) {
  const { t } = useI18n();
  const { families, loading } = useContext(FontCatalog);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const update = (key: "ui" | "content" | "code", family: string | null, face?: { family: string; fullName: string; postscriptName: string }) => {
    void onChange({ ui: null, content: null, code: null, ...fonts, [key]: family, [`${key}Face`]: face })
      .catch(error => pushToast(t("字体更新失败：{v0}", { v0: String(error) }), "error"));
  };
  const control = (key: "ui" | "content" | "code") => {
    const value = fonts?.[key] || null;
    const firstFamily = firstFontFamily(value);
    const selected = families?.find(family => family.family.toLocaleLowerCase() === firstFontFamily(value)?.toLocaleLowerCase());
    const allowed = (family: SystemFontFamily) => key !== "code" || family.faces.every(face => face.isMonospaced);
    const selectedFace = selected?.faces.find(face => face.postscriptName === fonts?.[`${key}Face`]?.postscriptName) || selected?.faces[0];
    const label = key === "ui" ? t(advanced ? "界面字体样式" : "字体") : t(key === "content" ? "内容字体" : "代码字体");
    const familyAria = t("{variant}{font}", { variant: variantLabel, font: key === "ui" ? t("界面字体") : label });
    const styleAria = t("{variant}{font}样式", { variant: variantLabel, font: key === "ui" ? t("界面字体") : label });
    const defaultLabel = t(key === "content" ? "跟随界面字体" : "系统");
    return <div className="settings-row" key={key}><div className="settings-row-copy"><div className="settings-row-title">{label}</div></div>
      <div className="appearance-font-controls">
        {loading ? <span role="status" aria-label={t("正在读取字体…")}><LoaderCircle className="settings-spin" size={13} aria-hidden="true" /></span> : <>
          {!(advanced && key === "ui") && (families?.length ? <SelectMenu className="settings-select appearance-font-picker" value={firstFamily ? cssFontFamily(firstFamily) : value || ""}
            ariaLabel={familyAria} align="end" disabled={!active} onValueChange={family => update(key, family || null)}>
            <option value="">{defaultLabel}</option>
            {families.filter(allowed).map(family => <option key={family.family} value={cssFontFamily(family.family)}>{family.family}</option>)}
          </SelectMenu> : <CustomFontFamily label={familyAria} defaultLabel={defaultLabel} value={value} onChange={family => update(key, family)} active={active} />)}
          {advanced && <SelectMenu className="settings-select appearance-font-style-picker" value={selectedFace?.postscriptName || ""} ariaLabel={styleAria} align="end"
            disabled={!active || !selected || !allowed(selected)} onValueChange={postscriptName => {
              const face = selected!.faces.find(face => face.postscriptName === postscriptName)!;
              update(key, cssFontFamily(selected!.family), face === selected!.faces[0] ? undefined : { family: selected!.family, fullName: face.fullName, postscriptName: face.postscriptName });
            }}>
            {!selected && <option value="">{t("常规字形")}</option>}
            {selected?.faces.map(face => <option key={face.postscriptName} value={face.postscriptName}>{face.styleName}</option>)}
          </SelectMenu>}
        </>}
      </div>
    </div>;
  };
  return <div data-font-variant={variant}>{advanced ? <>{control("ui")}{control("content")}{control("code")}</> : control("ui")}</div>;
}
