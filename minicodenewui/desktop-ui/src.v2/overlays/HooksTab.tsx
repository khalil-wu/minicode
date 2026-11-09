import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../i18n";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { openManagedSettingsTarget } from "../desktop/runtime";
import { SettingsSourceIcon } from "./SettingsSourceIcon";
import { Tip } from "../components/Tooltip";

type Hook = { key: string; event: string; handler_type: string; matcher: string | null; command: string | null; prompt: string | null; url: string | null;
  server?: string | null; tool?: string | null; input?: unknown;
  source_path: string; source: string; plugin_id: string | null; enabled: boolean; is_managed: boolean; current_hash: string; trust_status: string; timeout_seconds: number | null };
type Snapshot = { hooks: Hook[]; warnings: string[] };
const hookHandlerLabel = (hook: Hook): string => hook.handler_type === "mcp_tool"
  ? [hook.server, hook.tool].filter(Boolean).join(".") || hook.handler_type
  : hook.command || hook.url || hook.handler_type;
export function HooksTab({ active = true, toolbarHost }: { active?: boolean; toolbarHost?: HTMLElement | null }) {
  const { t } = useI18n();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(active);
  const [revision, setRevision] = useState(0);
  const readOperation = useRef<AbortController>();
  useEffect(() => {
    if (!active) return;
    const controller = readOperation.current = new AbortController();
    setError("");
    setLoading(true);
    const url = new URL("/api/settings/hooks", apiBase());
    void fetchWithTimeout(url, { headers: authHeaders(), signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<Snapshot>;
    }).then(value => { if (!controller.signal.aborted) setSnapshot(value); })
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [active, revision]);
  const update = async (updates: { key: string; enabled?: boolean; trusted_hash?: string }[]) => {
    readOperation.current?.abort();
    setLoading(false);
    setBusy(true); setError("");
    try {
      const response = await fetchWithTimeout(new URL("/api/settings/hooks", apiBase()), { method: "PUT", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify({ workspace_root: "", updates }) });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      setSnapshot(await response.json() as Snapshot); setError("");
    } catch (reason) { setError(String(reason)); }
    finally { readOperation.current?.abort(); setLoading(false); setBusy(false); }
  };
  const needsTrust = snapshot?.hooks.filter(hook => !hook.is_managed && ["untrusted", "modified"].includes(hook.trust_status)) || [];
  const groups = [
    { label: t("来自配置"), hooks: snapshot?.hooks.filter(hook => !["plugin", "project"].includes(hook.source)) || [] },
    { label: t("来自插件"), hooks: snapshot?.hooks.filter(hook => hook.source === "plugin") || [] },
    { label: t("来自项目"), hooks: snapshot?.hooks.filter(hook => hook.source === "project") || [] },
  ];
  const reload = <Tip content={t("重新加载钩子")}><button type="button" className="settings-icon-button settings-refresh-button settings-hooks-refresh" aria-label={t("重新加载钩子")} disabled={busy || loading} aria-busy={loading} onClick={() => setRevision(value => value + 1)}><SettingsSourceIcon icon="refresh" /></button></Tip>;
  return <>
    {toolbarHost && active && createPortal(reload, toolbarHost)}
    {!toolbarHost && reload}
    {error && <p role="alert">{error}</p>}
    {!snapshot && !error && <p role="status">{t("正在加载钩子…")}</p>}
    {snapshot?.warnings.map(warning => <p key={warning} className="settings-page-note">{warning}</p>)}
    {needsTrust.length > 0 && <div className="settings-row"><span>{t("钩子在沙箱外运行，可能不安全。")}</span><button type="button" className="settings-action-button" disabled={busy} onClick={() => void update(needsTrust.map(hook => ({ key: hook.key, trusted_hash: hook.current_hash })))}>{t("全部信任")}</button></div>}
    {snapshot?.hooks.length === 0 && <div className="settings-card"><div className="settings-row"><div className="settings-row-copy">
      <div className="settings-row-title">{t("未找到钩子")}</div><div className="settings-row-description">{t("已配置的钩子将显示在这里。")}</div>
    </div></div></div>}
    {groups.filter(group => group.hooks.length > 0).map(group => <section className="settings-group" key={group.label}><h3 className="settings-group-title">{group.label}</h3><div className="settings-card">{group.hooks.map(hook => <details key={hook.key} className="settings-detail-block">
      <summary className="settings-row"><span className="settings-row-copy"><span className="settings-row-title">{hook.event}</span><span className="settings-row-description">{hookHandlerLabel(hook)}</span></span>
        <button type="button" className="settings-toggle" role="switch" aria-label={`${hook.event}: ${hookHandlerLabel(hook)}`} disabled={busy || hook.is_managed || needsTrust.includes(hook)} title={hook.is_managed ? t("受管理的钩子始终开启。") : undefined} aria-checked={hook.enabled} data-active={hook.enabled} onClick={event => { event.preventDefault(); void update([{ key: hook.key, enabled: !hook.enabled }]); }}><span /></button>
      </summary>
      <div className="settings-page-note"><dl className="settings-hook-details"><dt>{t("来源")}</dt><dd>{hook.plugin_id || hook.source_path}</dd><dt>{t("钩子类型")}</dt><dd>{hook.handler_type}</dd>
        {hook.matcher && <><dt>{t("匹配规则")}</dt><dd>{hook.matcher}</dd></>}
        <dt>{t("处理程序")}</dt><dd><pre>{hook.command || hook.prompt || hook.url || hookHandlerLabel(hook)}</pre>{hook.handler_type === "mcp_tool" && hook.input !== undefined && <pre>{JSON.stringify(hook.input, null, 2)}</pre>}</dd>
        {hook.timeout_seconds !== null && <><dt>{t("超时")}</dt><dd>{hook.timeout_seconds}s</dd></>}
      </dl><button type="button" className="settings-action-button" onClick={() => { void openManagedSettingsTarget({ kind: "hook", hookKey: hook.key })?.catch(reason => setError(String(reason))); }}>{t("打开配置文件")}</button>
      {needsTrust.includes(hook) && <button type="button" className="settings-action-button" disabled={busy} onClick={() => void update([{ key: hook.key, trusted_hash: hook.current_hash }])}>{t("信任")}</button>}</div>
    </details>)}</div></section>)}
  </>;
}
