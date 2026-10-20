import { useEffect, useState, type ReactNode } from "react";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { SelectMenu } from "../components/SelectMenu";
import { openManagedSettingsTarget } from "../desktop/runtime";

type Config = { path: string; approval_policy: string | Record<string, unknown>; sandbox_mode: string; web_search: string; network_access: boolean; model_verbosity: string; model_reasoning_summary: string };
export function ConfigTab({ active, children }: { active: boolean; children?: ReactNode }) {
  const { t } = useI18n();
  const workspaceRoot = useAppStore(state => state.workingDirectory);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(); setError(""); setConfig(null);
    const url = new URL("/api/settings/agent-config", apiBase()); url.searchParams.set("workspace_root", workspaceRoot);
    void fetchWithTimeout(url, { headers: authHeaders(), signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<Config>;
    }).then(next => { if (!controller.signal.aborted) setConfig(next); }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [active, workspaceRoot]);
  const save = async (patch: Partial<Config>) => {
    setBusy(true); setError("");
    try {
      const response = await fetchWithTimeout(new URL("/api/settings/agent-config", apiBase()), { method: "PUT", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify({ workspace_root: workspaceRoot, ...patch }) });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const next = await response.json() as Config;
      if (useAppStore.getState().workingDirectory === workspaceRoot) setConfig(next);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  return <>
    <section className="settings-group"><div className="settings-section-heading settings-section-heading-row"><p className="settings-section-description">{t("为新聊天配置权限、网络访问和智能体回复。")}</p><button type="button" className="settings-action-button" disabled={!config} onClick={() => { void openManagedSettingsTarget({ kind: "config", workspaceRoot })?.catch(reason => setError(String(reason))); }}>{t("打开配置文件")}</button></div>
      {error && <p role="alert">{error}</p>}
      <div className="settings-card"><div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("审批策略")}</div><div className="settings-row-description">{t("选择 MiniCode 何时请求批准。")}</div></div><SelectMenu ariaLabel={t("审批策略")} disabled={!config || busy || typeof config.approval_policy !== "string"} value={typeof config?.approval_policy === "string" ? config.approval_policy : "granular"} onValueChange={value => { void save({ approval_policy: value }); }}><option value="on-request">{t("按需")}</option><option value="untrusted">{t("不受信任")}</option><option value="never">{t("从不")}</option>{typeof config?.approval_policy === "object" && <option value="granular">{t("自定义")}</option>}</SelectMenu></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("沙箱模式")}</div><div className="settings-row-description">{t("选择 MiniCode 运行命令时的访问范围。")}</div></div><SelectMenu ariaLabel={t("沙箱模式")} disabled={!config || busy} value={config?.sandbox_mode || "workspace-write"} onValueChange={value => { void save({ sandbox_mode: value }); }}><option value="read-only">{t("只读")}</option><option value="workspace-write">{t("工作区写入")}</option><option value="danger-full-access">{t("完整访问权限")}</option>{config?.sandbox_mode === "external-sandbox" && <option value="external-sandbox">{t("外部沙箱")}</option>}</SelectMenu></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("允许网络访问")}</div><div className="settings-row-description">{t("允许工作区写入沙箱访问网络。")}</div></div><button type="button" className="settings-toggle" role="switch" aria-label={t("允许网络访问")} disabled={!config || busy || config.sandbox_mode !== "workspace-write"} aria-checked={config?.network_access ?? false} data-active={config?.network_access ?? false} onClick={() => { void save({ network_access: !config!.network_access }); }}><span /></button></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("网页搜索")}</div><div className="settings-row-description">{t("选择 MiniCode 如何访问网页。")}</div></div><SelectMenu ariaLabel={t("网页搜索")} disabled={!config || busy} value={config?.web_search || "cached"} onValueChange={value => { void save({ web_search: value }); }}><option value="disabled">{t("已禁用")}</option><option value="cached">{t("缓存")}</option><option value="indexed">Indexed</option><option value="live">{t("实时")}</option></SelectMenu></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("回复详细程度")}</div><div className="settings-row-description">{t("选择 MiniCode 在回复中包含多少细节。")}</div></div><SelectMenu ariaLabel={t("回复详细程度")} disabled={!config || busy} value={config?.model_verbosity || "medium"} onValueChange={value => { void save({ model_verbosity: value }); }}><option value="low">{t("低")}</option><option value="medium">{t("中")}</option><option value="high">{t("高")}</option></SelectMenu></div>
        <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("推理摘要")}</div><div className="settings-row-description">{t("选择 MiniCode 如何概述推理过程。")}</div></div><SelectMenu ariaLabel={t("推理摘要")} disabled={!config || busy} value={config?.model_reasoning_summary || "auto"} onValueChange={value => { void save({ model_reasoning_summary: value }); }}><option value="auto">{t("自动")}</option><option value="concise">{t("简洁")}</option><option value="detailed">{t("详细")}</option><option value="none">{t("无")}</option></SelectMenu></div>
      </div>
    </section>
    {children}
  </>;
}
