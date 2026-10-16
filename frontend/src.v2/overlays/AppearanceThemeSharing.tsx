import { useRef, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Copy, Download, X } from "../lib/icons";
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
  onImport: (variant: AppearanceVariant, snapshot: CodeThemeAppearance) => void | Promise<void>;
}

function ThemeImportDialog({ variant, exampleValue, onClose, onImport, trigger }: Omit<AppearanceThemeSharingProps, "snapshot"> & {
  exampleValue: string;
  onClose: () => void;
  trigger: RefObject<HTMLButtonElement>;
}) {
  const { t } = useI18n();
  const [value, setValue] = useState("");
  const dialog = useFocusTrap(true, trigger);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const valid = canImportThemeString(value, variant);
  const submit = async () => {
    try {
      await onImport(variant, parseThemeString(value, variant));
    } catch (error) {
      console.error("Theme import failed", error);
      pushToast(t("无法导入{variant}主题", { variant: variantLabel }), "error");
      return;
    }
    onClose();
    pushToast(t("已导入{variant}主题", { variant: variantLabel }), "success");
  };

  return createPortal(<div className="appearance-theme-import-backdrop" onPointerDown={event => {
    if (event.target === event.currentTarget) onClose();
  }}>
    <div ref={dialog} className="appearance-theme-import-dialog" role="dialog" aria-modal="true" aria-label={t("导入主题")}
      tabIndex={-1} onKeyDown={event => {
        if (event.key !== "Escape") return;
        event.preventDefault(); event.stopPropagation(); onClose();
      }}>
      <form onSubmit={event => { event.preventDefault(); if (valid) void submit(); }}>
        <div className="appearance-theme-import-heading">
          <h2>{t("导入主题")}</h2>
          <button type="button" className="appearance-theme-share-button" aria-label={t("关闭")} onClick={onClose}><X size={18} /></button>
        </div>
        <input type="text" className="appearance-theme-import-input" aria-label={t("{variant}主题分享字符串", { variant: variantLabel })}
          autoFocus spellCheck={false} placeholder={exampleValue} value={value} onChange={event => setValue(event.target.value)} />
        <div className="appearance-theme-import-actions">
          <button type="button" className="appearance-theme-import-cancel" onClick={onClose}>{t("取消")}</button>
          <button type="submit" className="appearance-theme-import-submit" disabled={!valid}>{t("导入主题")}</button>
        </div>
      </form>
    </div>
  </div>, document.body);
}

/** Codex general-settings Xa/$a: import dialog and copy action per variant. */
export function AppearanceThemeSharing({ variant, snapshot, onImport }: AppearanceThemeSharingProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const variantLabel = t(variant === "light" ? "浅色" : "深色");
  const importLabel = t("导入{variant}主题", { variant: variantLabel });
  const copyLabel = t("复制{variant}主题", { variant: variantLabel });
  const exampleValue = exportThemeString(snapshot, variant);
  return <div className="appearance-theme-sharing">
    <button ref={trigger} type="button" className="appearance-theme-share-button" aria-label={importLabel} title={importLabel}
      onClick={() => setOpen(true)}><Download size={18} /></button>
    <button type="button" className="appearance-theme-share-button" aria-label={copyLabel} title={copyLabel}
      onClick={() => { void copyText(exampleValue, t("{variant}主题", { variant: variantLabel }), t("已复制{variant}主题", { variant: variantLabel })); }}><Copy size={18} /></button>
    {open && <ThemeImportDialog key={variant} variant={variant} exampleValue={exampleValue} onClose={() => setOpen(false)} onImport={onImport} trigger={trigger} />}
  </div>;
}
