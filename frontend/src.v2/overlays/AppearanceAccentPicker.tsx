import { useRef, useState } from "react";
import { ContextMenu } from "../components/ContextMenu";
import { useI18n } from "../i18n";
import type { AppearanceVariant } from "../lib/appearance-colors";
import { SettingsSourceIcon } from "./SettingsSourceIcon";

// Source za/xr.options and jos/DZ. Account options stay disabled when the
// ChatGPT account accent consumer is unavailable; Custom edits local colors.
const accountAccents = [
  { id: "default", label: "默认", color: "neutral" },
  { id: "blue", label: "蓝色", color: "#3566f0" },
  { id: "green", label: "绿色", color: "#04bb9f" },
  { id: "yellow", label: "黄色", color: "#ffcc38" },
  { id: "pink", label: "粉色", color: "#fa70ab" },
  { id: "orange", label: "橙色", color: "#ff8066" },
  { id: "purple", label: "紫色", color: "#a25bff" },
  { id: "black", label: "黑色", color: "neutral" },
] as const;

export function AppearanceAccentPicker({ variant, onCustomSelect }: { variant: AppearanceVariant; onCustomSelect: () => void }) {
  const { t } = useI18n();
  const trigger = useRef<HTMLButtonElement>(null);
  const [position, setPosition] = useState<{ x: number; y: number } | null>(null);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const ariaLabel = t("{variant}强调色", { variant: variantLabel });
  const open = () => {
    const bounds = trigger.current!.getBoundingClientRect();
    setPosition({ x: bounds.right - 180, y: bounds.bottom + 4 });
  };
  return <div className="mc-select-menu settings-select appearance-accent-picker" data-open={Boolean(position)}>
    <button ref={trigger} type="button" className="mc-select-trigger" aria-label={ariaLabel}
      aria-haspopup="menu" aria-expanded={Boolean(position)} onClick={() => position ? setPosition(null) : open()}
      onKeyDown={event => { if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); open(); } }}>
      <span className="mc-select-trigger-value"><span className="mc-select-trigger-label">{t("自定义")}</span></span>
      <SettingsSourceIcon icon="chevronDown" size={14} />
    </button>
    {position && <ContextMenu position={position} anchor={trigger.current!} className="appearance-accent-menu" ariaLabel={ariaLabel} onClose={() => setPosition(null)} returnFocusTo={trigger.current!}
      items={[
        ...accountAccents.map(accent => ({
          label: t(accent.id === "black" && variant === "dark" ? "白色" : accent.label),
          icon: <span className="appearance-accent-menu-swatch" style={{ background: accent.color === "neutral" ? variant === "dark" ? "#ffffff" : "#000000" : accent.color }} />,
          disabled: true, checked: false, checkType: "radio" as const,
        })),
        { label: t("自定义"), checked: true, checkType: "radio", onClick: onCustomSelect },
      ]} />}
  </div>;
}
