import { useEffect, useRef, useState, type CSSProperties } from "react";
import { ContextMenu } from "../components/ContextMenu";
import { ChevronDown } from "../lib/icons";
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
  const [position, setPosition] = useState<{ x: number; y: number } | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const select = async (id: CodeThemeId) => {
    try {
      await onSelect(id);
    } catch (error) {
      console.error("Code theme selection failed", error);
      pushToast(t("无法应用{variant}代码主题", { variant: variantLabel }), "error");
    }
  };

  const themeItem = (theme: typeof themes[number]) => ({ label: theme.label,
    icon: <CodeThemePreview id={theme.id} label={theme.label} variant={variant} colors={colors} />,
    checked: value === theme.id, checkType: "radio" as const, onClick: () => { void select(theme.id); } });
  return <div className="mc-select-menu settings-select appearance-code-theme-picker">
    <button ref={trigger} type="button" className="mc-select-trigger" aria-label={t("{variant}代码主题", { variant: variantLabel })}
      aria-haspopup="menu" aria-expanded={Boolean(position)} onClick={() => {
        const bounds = trigger.current!.getBoundingClientRect();
        setPosition(position ? null : { x: bounds.right - 220, y: bounds.bottom + 4 });
      }}>
      <CodeThemePreview id={value} label={themes.find(theme => theme.id === value)!.label} variant={variant} colors={colors} />
      <span className="mc-select-trigger-label">{themes.find(theme => theme.id === value)!.label}</span><ChevronDown size={14} />
    </button>
    {position && <ContextMenu position={position} onClose={() => setPosition(null)} returnFocusTo={trigger.current!}
      ariaLabel={t("{variant}代码主题", { variant: variantLabel })} items={[
        themeItem(themes.find(theme => theme.id === "codex")!),
        { label: t("Codex 主题"), submenuMaxHeight: 480, submenu: themes.filter(theme => theme.id !== "codex").map(themeItem) },
      ]} />}
  </div>;
}
