import { useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useI18n } from "../i18n";
import { useFocusTrap } from "../hooks/useFocusTrap";

export type MarketplaceAddParams = { source: string; ref_name: string | null; sparse_paths: string[] | null };

export function PluginMarketplaceDialog({ onAdd, onClose }: { onAdd: (params: MarketplaceAddParams) => Promise<void>; onClose: () => void }) {
  const { t } = useI18n();
  const [source, setSource] = useState("");
  const [refName, setRefName] = useState("");
  const [sparsePaths, setSparsePaths] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const dialog = useFocusTrap(true);
  const submit = async () => {
    setSaving(true);
    setError("");
    const paths = sparsePaths.split(/[\n,]+/).map(path => path.trim()).filter(Boolean);
    try {
      await onAdd({ source: source.trim(), ref_name: refName.trim() || null, sparse_paths: paths.length ? paths : null });
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };
  return createPortal(<div ref={dialog} className="plugin-marketplace-backdrop" onClick={() => { if (!saving) onClose(); }}>
    <form className="plugin-marketplace-dialog" role="dialog" aria-modal="true" aria-labelledby="plugin-marketplace-title" onClick={event => event.stopPropagation()}
      onSubmit={event => { event.preventDefault(); void submit(); }} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!saving) onClose(); } }}>
      <header><h2 id="plugin-marketplace-title">{t("添加插件市场")}</h2><p>{t("从 GitHub 仓库、Git URL 或本地文件夹添加。")} <a href="https://developers.openai.com/codex/plugins/build" target="_blank" rel="noreferrer">{t("了解更多")}</a></p></header>
      {!saving && <button className="plugin-marketplace-close" type="button" aria-label={t("关闭")} onClick={onClose}><X size={16} /></button>}
      <div className="plugin-marketplace-fields">
        <label>{t("来源")}<input autoFocus value={source} onChange={event => setSource(event.currentTarget.value)} placeholder="openai/plugins or git@github.com:org/repo.git" disabled={saving} required /></label>
        <label>{t("Git 引用")}<input value={refName} onChange={event => setRefName(event.currentTarget.value)} placeholder="main" disabled={saving} /></label>
        <label>{t("稀疏路径")}<textarea value={sparsePaths} onChange={event => setSparsePaths(event.currentTarget.value)} placeholder="plugins/codex" disabled={saving} /></label>
        {error && <div className="plugin-marketplace-submit-error" role="alert">{error}</div>}
      </div>
      <footer><button type="button" className="settings-action-button" disabled={saving} onClick={onClose}>{t("取消")}</button><button type="submit" className="plugin-primary-button" disabled={saving || !source.trim()}>{saving ? t("添加中…") : t("添加市场")}</button></footer>
    </form>
  </div>, document.body);
}
