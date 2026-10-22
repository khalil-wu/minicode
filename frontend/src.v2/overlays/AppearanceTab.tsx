import { useState, type KeyboardEvent } from "react";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { EditorPreferencesSettings } from "./EditorPreferencesSettings";
import { AppearanceColors, AppearanceWindowCapabilitiesProvider, useAppearanceTranslucency } from "./AppearanceColors";
import { AppearanceFontCatalogProvider } from "./AppearanceFonts";
import { AppearanceModePreview } from "./AppearanceModePreview";
import { defaultThemePreferences } from "../lib/appearance-themes";
import { NumberInput } from "../components/NumberInput";
import { pushToast } from "./ToastContainer";

const onRadioGroupKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
  if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]'));
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (current + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1) + buttons.length) % buttons.length;
  buttons[next].focus(); buttons[next].click();
};
const THEMES = [{ id: "system", label: "系统" }, { id: "light", label: "浅色" }, { id: "dark", label: "深色" }] as const;

function AppearanceSettings() {
  const { t } = useI18n();
  const themeMode = useAppStore(state => state.themeMode), resolved = useAppStore(state => state.resolvedTheme);
  const themes = useAppStore(state => state.appearanceThemes);
  const textScale = useAppStore(state => state.textScale), codeScale = useAppStore(state => state.codeTextScale), codeBase = useAppStore(state => state.codeFontBase);
  const motion = useAppStore(state => state.reducedMotionPreference), pointer = useAppStore(state => state.pointerCursors), markers = useAppStore(state => state.diffMarkerStyle);
  const supportsTranslucency = useAppearanceTranslucency();
  const [separateModes, setSeparateModes] = useState(false);
  const variants = separateModes ? ["light", "dark"] as const : [resolved];
  const advancedThemeChanged = (["light", "dark"] as const).some(variant => {
    const theme = themes[variant]; if (!theme) return false;
    const defaults = defaultThemePreferences(variant);
    return theme.contrast !== defaults.contrast || supportsTranslucency && theme.opaqueWindows !== defaults.opaqueWindows
      || Boolean(theme.fonts?.uiFace || theme.fonts?.contentFace || theme.fonts?.codeFace || theme.fonts?.content || theme.fonts?.code);
  });
  const canReset = separateModes || textScale !== 1 || codeScale * codeBase !== 12 || motion !== "system" || pointer || markers !== "color" || advancedThemeChanged;
  const reset = () => {
    try { useAppStore.getState().resetAdvancedAppearance(supportsTranslucency); setSeparateModes(false); }
    catch (error) { pushToast(t("部分外观设置未能恢复。请重试：{error}", { error: error instanceof Error ? error.message : String(error) }), "error"); }
  };
  return <>
    <section className="settings-group settings-appearance-theme">
      <h3 className="settings-group-title">{t("视觉风格")}</h3>
      <div className="settings-card"><div className="settings-row settings-mode-row">
        <div className="settings-row-copy"><div className="settings-row-title">{t("模式")}</div></div>
        <div className="settings-theme-grid" role="radiogroup" aria-label={t("应用主题")} onKeyDown={onRadioGroupKeyDown}>
          {THEMES.map(({ id, label }) => <button key={id} type="button" className="settings-theme-card" data-theme-preview={id}
            data-active={String(themeMode === id)} role="radio" aria-checked={themeMode === id} tabIndex={themeMode === id ? 0 : -1} aria-label={t(label)}
            onClick={() => useAppStore.getState().setThemeMode(id)}>
            <span className="settings-theme-preview" aria-hidden="true"><AppearanceModePreview mode={id} /></span>
          </button>)}
        </div>
      </div></div>
      {variants.map(variant => <AppearanceColors key={variant} variant={variant} separateModes={separateModes} />)}
    </section>
    <EditorPreferencesSettings canReset={canReset} onReset={reset}>
      <section className="settings-group"><div className="settings-card">
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("界面字号")}</div><div className="settings-row-description">{t("调整应用界面的基础字号")}</div></div>
          <span className="settings-row-control"><NumberInput aria-label={t("界面字号")} min={11} max={16} step="any" value={14 * textScale} onCommit={value => useAppStore.getState().setTextScale(value / 14)} /> px</span></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("代码字号")}</div><div className="settings-row-description">{t("调整聊天和差异中代码的基础字号")}</div></div>
          <span className="settings-row-control"><NumberInput aria-label={t("代码字号")} min={8} max={24} step="any" value={codeBase * codeScale} onCommit={value => useAppStore.getState().setCodeTextScale(value / codeBase)} /> px</span></div>
      </div></section>
      <section className="settings-group"><div className="settings-card">
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("减少动态效果")}</div><div className="settings-row-description">{t("减少动画，或跟随系统设置")}</div></div>
          <div className="appearance-segmented" role="radiogroup" aria-label={t("减少动态效果")} onKeyDown={onRadioGroupKeyDown}>
            {([{ id: "system", label: t("系统") }, { id: "on", label: t("动效开启") }, { id: "off", label: t("动效关闭") }] as const).map(item => <button type="button" key={item.id} role="radio" aria-checked={motion === item.id}
              tabIndex={motion === item.id ? 0 : -1} onClick={() => useAppStore.getState().setReducedMotionPreference(item.id)}>{item.label}</button>)}
          </div>
        </div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("分别设置浅色和深色模式")}</div><div className="settings-row-description">{t("为每种模式选择主题、颜色和字体")}</div></div>
          <button type="button" className="settings-toggle" role="switch" aria-label={t("分别设置浅色和深色模式")} aria-checked={separateModes} data-active={separateModes} onClick={() => setSeparateModes(!separateModes)}><span /></button>
        </div>
      </div></section>
      {variants.map(variant => <AppearanceColors advanced key={variant} variant={variant} separateModes={separateModes} />)}
      <section className="settings-group"><div className="settings-card">
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("差异标记")}</div><div className="settings-row-description">{t("使用颜色或 +/- 标记显示更改")}</div></div>
          <div className="appearance-segmented" role="radiogroup" aria-label={t("差异标记")} onKeyDown={onRadioGroupKeyDown}>
            {([{ id: "color", label: t("颜色") }, { id: "symbols", label: "+/-" }] as const).map(item => <button key={item.id} type="button" role="radio" aria-checked={markers === item.id} tabIndex={markers === item.id ? 0 : -1}
              onClick={() => useAppStore.getState().setDiffMarkerStyle(item.id)}>{item.label}</button>)}
          </div>
        </div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("使用手形指针")}</div><div className="settings-row-description">{t("悬停在可交互元素上时显示手形指针")}</div></div>
          <button type="button" className="settings-toggle" role="switch" aria-label={t("使用手形指针")} aria-checked={pointer} data-active={pointer} onClick={() => useAppStore.getState().setPointerCursors(!pointer)}><span /></button>
        </div>
      </div></section>
    </EditorPreferencesSettings>
  </>;
}

export const AppearanceTab = () => <AppearanceFontCatalogProvider><AppearanceWindowCapabilitiesProvider><AppearanceSettings /></AppearanceWindowCapabilitiesProvider></AppearanceFontCatalogProvider>;
