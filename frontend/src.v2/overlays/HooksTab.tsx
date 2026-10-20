import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { RefreshCw } from "../lib/icons";
import { useI18n } from "../i18n";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { openManagedSettingsTarget } from "../desktop/runtime";
import { openPreferredWebLink } from "../lib/open-preferred-target";

type Hook = { key: string; event: string; handler_type: string; matcher: string | null; command: string | null; prompt: string | null; url: string | null;
  source_path: string; source: string; plugin_id: string | null; enabled: boolean; is_managed: boolean; current_hash: string; trust_status: string; timeout_seconds: number | null };
type Snapshot = { hooks: Hook[]; warnings: string[] };
export function HooksTab({ active = true, toolbarHost }: { active?: boolean; toolbarHost?: HTMLElement | null }) {
  const { t } = useI18n();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setError("");
    const url = new URL("/api/settings/hooks", apiBase());
    void fetchWithTimeout(url, { headers: authHeaders(), signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<Snapshot>;
    }).then(value => { if (!controller.signal.aborted) setSnapshot(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [active, revision]);
  const update = async (updates: { key: string; enabled?: boolean; trusted_hash?: string }[]) => {
    setBusy(true); setError("");
    try {
      const response = await fetchWithTimeout(new URL("/api/settings/hooks", apiBase()), { method: "PUT", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify({ workspace_root: "", updates }) });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      setSnapshot(await response.json() as Snapshot);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const needsTrust = snapshot?.hooks.filter(hook => !hook.is_managed && ["untrusted", "modified"].includes(hook.trust_status)) || [];
  const groups = [
    { label: t("来自配置"), hooks: snapshot?.hooks.filter(hook => !["plugin", "project"].includes(hook.source)) || [] },
    { label: t("来自插件"), hooks: snapshot?.hooks.filter(hook => hook.source === "plugin") || [] },
    { label: t("来自项目"), hooks: snapshot?.hooks.filter(hook => hook.source === "project") || [] },
  ];
  const reload = <button type="button" className="settings-icon-button" aria-label={t("重新加载钩子")} disabled={busy || (!snapshot && !error)} onClick={() => setRevision(value => value + 1)}><RefreshCw size={16} /></button>;
  return <>
    {toolbarHost && active && createPortal(reload, toolbarHost)}
    <div className="settings-section-heading settings-section-heading-row"><p className="settings-section-description">{t("管理来自配置和已启用插件的生命周期钩子。")}{" "}<a href="https://developers.openai.com/codex/hooks" onClick={event => { event.preventDefault(); void openPreferredWebLink("https://developers.openai.com/codex/hooks"); }}>{t("了解更多")}</a></p>
      {!toolbarHost && reload}
    </div>
    {error && <p role="alert">{error}</p>}
    {!snapshot && !error && <p role="status">{t("正在加载钩子…")}</p>}
    {snapshot?.warnings.map(warning => <p key={warning} className="settings-page-note">{warning}</p>)}
    {needsTrust.length > 0 && <div className="settings-row"><span>{t("钩子在沙箱外运行，可能不安全。")}</span><button type="button" className="settings-action-button" disabled={busy} onClick={() => void update(needsTrust.map(hook => ({ key: hook.key, trusted_hash: hook.current_hash })))}>{t("全部信任")}</button></div>}
    {snapshot?.hooks.length === 0 && <div className="settings-load-state"><div className="settings-load-state-copy"><strong>{t("未找到钩子")}</strong><span>{t("已配置的钩子将显示在这里。")}</span></div></div>}
    {groups.filter(group => group.hooks.length > 0).map(group => <section className="settings-group" key={group.label}><h3 className="settings-group-title">{group.label}</h3><div className="settings-card">{group.hooks.map(hook => <details key={hook.key} className="settings-detail-block">
      <summary className="settings-row"><span className="settings-row-copy"><span className="settings-row-title">{hook.event}</span><span className="settings-row-description">{hook.command || hook.url || hook.handler_type}</span></span>
        <button type="button" className="settings-toggle" role="switch" aria-label={`${hook.event}: ${hook.command || hook.handler_type}`} disabled={busy || hook.is_managed || needsTrust.includes(hook)} title={hook.is_managed ? t("受管理的钩子始终开启。") : undefined} aria-checked={hook.enabled} data-active={hook.enabled} onClick={event => { event.preventDefault(); void update([{ key: hook.key, enabled: !hook.enabled }]); }}><span /></button>
      </summary>
      <div className="settings-page-note"><dl className="settings-hook-details"><dt>{t("来源")}</dt><dd>{hook.plugin_id || hook.source_path}</dd><dt>{t("钩子类型")}</dt><dd>{hook.handler_type}</dd>
        {hook.matcher && <><dt>{t("匹配规则")}</dt><dd>{hook.matcher}</dd></>}
        <dt>{t("处理程序")}</dt><dd><pre>{hook.command || hook.prompt || hook.url}</pre></dd>
        {hook.timeout_seconds !== null && <><dt>{t("超时")}</dt><dd>{hook.timeout_seconds}s</dd></>}
      </dl><button type="button" className="settings-action-button" onClick={() => { void openManagedSettingsTarget({ kind: "hook", hookKey: hook.key })?.catch(reason => setError(String(reason))); }}>{t("打开配置文件")}</button>
      {needsTrust.includes(hook) && <button type="button" className="settings-action-button" disabled={busy} onClick={() => void update([{ key: hook.key, trusted_hash: hook.current_hash }])}>{t("信任")}</button>}</div>
    </details>)}</div></section>)}
  </>;
}
