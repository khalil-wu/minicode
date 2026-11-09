import { useEffect, useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { X } from "../lib/icons";
import { SettingsSourceIcon } from "./SettingsSourceIcon";
import { copyText } from "../lib/clipboard";
import { canImportThemeString, exportThemeString, parseThemeString, type CodeThemeAppearance } from "../lib/theme-sharing";
import type { AppearanceVariant } from "../lib/appearance-colors";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { useI18n } from "../i18n";
import { pushToast } from "./ToastContainer";
import "./AppearanceThemeSharing.css";

export interface AppearanceThemeSharingProps {
  variant: AppearanceVariant;
  snapshot: CodeThemeAppearance;
  onImport: (variant: AppearanceVariant, snapshot: CodeThemeAppearance) => boolean | Promise<boolean>;
  active?: boolean;
}

function ThemeImportDialog({ variant, exampleValue, onClose, onSubmit, trigger, value, onValueChange, pending }: {
  variant: AppearanceVariant;
  exampleValue: string;
  onClose: () => void;
  onSubmit: () => Promise<void>;
  trigger: RefObject<HTMLButtonElement>;
  value: string;
  onValueChange: (value: string) => void;
  pending: boolean;
}) {
  const { t } = useI18n();
  const dialog = useFocusTrap(true, trigger);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const valid = canImportThemeString(value, variant);

  return createPortal(<div className="appearance-theme-import-backdrop" onPointerDown={event => {
    if (event.target === event.currentTarget && !pending) onClose();
  }}>
    <div ref={dialog} className="appearance-theme-import-dialog" role="dialog" aria-modal="true" aria-label={t("导入主题")}
      tabIndex={-1} onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key !== "Escape") return;
        event.preventDefault(); event.stopPropagation(); if (!pending) onClose();
      }}>
      <form onSubmit={event => { event.preventDefault(); if (valid && !pending) void onSubmit(); }}>
        <div className="appearance-theme-import-heading">
          <h2>{t("导入主题")}</h2>
          <button type="button" className="appearance-theme-share-button" aria-label={t("关闭")} disabled={pending} onClick={onClose}><X size={18} /></button>
        </div>
        <input type="text" className="appearance-theme-import-input" aria-label={t("{variant}主题分享字符串", { variant: variantLabel })}
          autoFocus spellCheck={false} disabled={pending} placeholder={exampleValue} value={value} onChange={event => onValueChange(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && (event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault(); }} />
        <div className="appearance-theme-import-actions">
          <button type="button" className="appearance-theme-import-cancel" disabled={pending} onClick={onClose}>{t("取消")}</button>
          <button type="submit" className="appearance-theme-import-submit" disabled={!valid || pending} aria-busy={pending}>{t("导入主题")}</button>
        </div>
      </form>
    </div>
  </div>, document.body);
}

/** Import dialog and copy action per variant. */
export function AppearanceThemeSharing({ variant, snapshot, onImport, active = true }: AppearanceThemeSharingProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [pending, setPending] = useState(false);
  useEffect(() => { if (!active) setOpen(false); }, [active]);
  const trigger = useRef<HTMLButtonElement>(null);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const importLabel = t("导入{variant}主题", { variant: variantLabel });
  const copyLabel = t("复制{variant}主题", { variant: variantLabel });
  const exampleValue = exportThemeString(snapshot, variant);
  const close = () => { setOpen(false); setValue(""); };
  // The import value and its in-flight write live outside the modal: hiding a
  // retained settings page retires only the portal, not that state.
  const submit = async () => {
    setPending(true);
    let committed: boolean;
    try {
      committed = await onImport(variant, parseThemeString(value, variant));
    } catch (error) {
      console.error("Theme import failed", error);
      pushToast(t("无法导入{variant}主题", { variant: variantLabel }), "error");
      return;
    } finally { setPending(false); }
    if (!committed) {
      pushToast(t("主题已更改，未导入{variant}主题。", { variant: variantLabel }), "info");
      return;
    }
    close();
    pushToast(t("已导入{variant}主题", { variant: variantLabel }), "success");
  };
  return <div className="appearance-theme-sharing">
    <button ref={trigger} type="button" className="appearance-theme-share-button" aria-label={importLabel} title={importLabel}
      onClick={() => setOpen(true)}><SettingsSourceIcon icon="themeImport" size={20} /></button>
    <button type="button" className="appearance-theme-share-button" aria-label={copyLabel} title={copyLabel}
      onClick={() => { void copyText(exampleValue, t("{variant}主题", { variant: variantLabel }), t("已复制{variant}主题", { variant: variantLabel })); }}><SettingsSourceIcon icon="themeCopy" size={20} /></button>
    {active && open && <ThemeImportDialog key={variant} variant={variant} exampleValue={exampleValue} onClose={close} onSubmit={submit}
      value={value} onValueChange={setValue} pending={pending} trigger={trigger} />}
  </div>;
}
