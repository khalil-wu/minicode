import { useEffect, useState, type CSSProperties } from "react";
import { SelectMenu } from "../components/SelectMenu";
import { useI18n } from "../i18n";
import type { AppearanceColorValues, AppearanceVariant } from "../lib/appearance-colors";
import { codeThemesForVariant, loadCodeThemeSeed, type CodeThemeId } from "../lib/code-themes";
import { pushToast } from "./ToastContainer";
import "./AppearanceCodeThemePicker.css";

export interface AppearanceCodeThemePickerProps {
  variant: AppearanceVariant;
  colors: AppearanceColorValues;
  value: CodeThemeId;
  onSelect: (id: CodeThemeId) => Promise<void>;
}

function CodeThemePreview({ id, label, variant, colors }: {
  id: CodeThemeId;
  label: string;
  variant: AppearanceVariant;
  colors: AppearanceColorValues;
}) {
  const { t } = useI18n();
  const [seed, setSeed] = useState<AppearanceColorValues>();
  useEffect(() => {
    void loadCodeThemeSeed(id, variant).then(setSeed).catch(error => {
      console.error("Code theme preview load failed", error);
      pushToast(t("加载{theme}主题预览失败", { theme: label }), "error");
    });
  }, [id, label, variant, t]);
  const preview = seed ?? colors;
  // Source Un/f0s uses current edited colors in the trigger, and the actual
  // candidate seed in the menu. The menu is portalled outside the select.
  const style = {
    "--code-theme-preview-accent": preview.accent,
    "--code-theme-preview-surface": preview.surface,
    "--code-theme-preview-border": `color-mix(in srgb, ${preview.ink} 16%, ${preview.surface})`,
    "--code-theme-current-accent": colors.accent,
    "--code-theme-current-surface": colors.surface,
    "--code-theme-current-border": `color-mix(in srgb, ${colors.ink} 16%, ${colors.surface})`,
  } as CSSProperties;
  return <span className="appearance-code-theme-preview" data-theme-id={id} style={style} aria-hidden="true">Aa</span>;
}

/** Source eo/to/io: mode-compatible directory themes, previews and selection. */
export function AppearanceCodeThemePicker({ variant, colors, value, onSelect }: AppearanceCodeThemePickerProps) {
  const { t } = useI18n();
  const themes = codeThemesForVariant(variant);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const select = async (id: CodeThemeId) => {
    try {
      await onSelect(id);
    } catch (error) {
      console.error("Code theme selection failed", error);
      pushToast(t("无法应用{variant}代码主题", { variant: variantLabel }), "error");
    }
  };

  return <SelectMenu className="settings-select appearance-code-theme-picker" align="end" menuMaxHeight={320}
    ariaLabel={t("{variant}代码主题", { variant: variantLabel })} value={value} onValueChange={id => { void select(id as CodeThemeId); }}
    renderOptionIcon={id => <CodeThemePreview key={`${variant}:${id}`} id={id as CodeThemeId}
      label={themes.find(theme => theme.id === id)!.label} variant={variant} colors={colors} />}>
    {themes.map(theme => <option key={theme.id} value={theme.id}>{theme.label}</option>)}
  </SelectMenu>;
}
