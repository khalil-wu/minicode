import { useEffect, useState, type ReactNode } from "react";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { SelectMenu } from "../components/SelectMenu";
import { openManagedSettingsTarget } from "../desktop/runtime";
import "./ConfigTab.css";

export type AgentConfigFields = {
  approval_policy: string | Record<string, unknown>;
  sandbox_mode: string;
  network_access: boolean;
  web_search: string;
  model_verbosity: string | null;
  model_reasoning_summary: string;
};
type Field = keyof AgentConfigFields;
type ConfigSource = { type: string; display: string; file?: string | null; profile?: string | null };
type ConfigOrigin = { source: ConfigSource; version: string };
type ConfigScope = { id: "user" | "managed"; kind: "user" | "managed"; path: string | null;
  version: string; values: Partial<AgentConfigFields>; readonly: boolean; source: ConfigSource };
type WriteField = { requested_value: unknown; effective_value: unknown; overriding_source?: ConfigOrigin };
export type AgentConfigSnapshot = AgentConfigFields & {
  path: string;
  effective: AgentConfigFields;
  scopes: ConfigScope[];
  origins: Partial<Record<Field, ConfigOrigin>>;
  requirements: { allowed_approval_policies?: Array<string | Record<string, unknown>>;
    allowed_sandbox_modes?: string[]; allowed_web_search_modes?: string[] };
  write?: { status: "applied" | "overridden"; path: string; version: string; fields: Partial<Record<Field, WriteField>> };
};
type ConfigPage = { snapshot: AgentConfigSnapshot | null; scopeId: "user" | "managed"; loading: boolean;
  loadError: string; openError: { scopeId: string; message: string } | null; pending: Field[];
  errors: Partial<Record<Field, string>>; overrides: Partial<Record<Field, WriteField>> };
const newPage = (): ConfigPage => ({ snapshot: null, scopeId: "user", loading: false, loadError: "", openError: null,
  pending: [], errors: {}, overrides: {} });
const managedSources = new Set(["policy", "mdm", "session_flags", "legacy_managed_config_file", "legacy_managed_config_mdm", "enterprise_managed"]);
const modelDefault = "model-default";
const errorText = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
let pendingConfigWrite: Promise<void> = Promise.resolve();
const readConfig = async (workspaceRoot: string, signal?: AbortSignal): Promise<AgentConfigSnapshot> => {
  const url = new URL("/api/settings/agent-config", apiBase());
  url.searchParams.set("workspace_root", workspaceRoot);
  const response = await fetchWithTimeout(url, { headers: authHeaders(), signal });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<AgentConfigSnapshot>;
};

export function ConfigTab({ active, children }: { active: boolean; children?: ReactNode }) {
  const { t } = useI18n();
  const workspaceRoot = useAppStore(state => state.workingDirectory);
  const [pages, setPages] = useState<Record<string, ConfigPage>>({});
  const updatePage = (owner: string, update: (page: ConfigPage) => ConfigPage) => {
    setPages(current => ({ ...current, [owner]: update(current[owner] ?? newPage()) }));
  };
  const page = pages[workspaceRoot];
  const config = page?.snapshot;
  const requestedScope = page?.scopeId ?? "user";
  const scope = config?.scopes.find(item => item.id === requestedScope) ?? config?.scopes[0];
  const scopeId = scope?.id ?? requestedScope;
  const effective = config?.effective;
  const values = scope?.values;
  const value = <K extends Field>(field: K): AgentConfigFields[K] | undefined => values?.[field] ?? effective?.[field];

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    updatePage(workspaceRoot, current => ({ ...current, loading: true, loadError: "" }));
    void readConfig(workspaceRoot, controller.signal).then(next => {
      if (!controller.signal.aborted) updatePage(workspaceRoot, current => ({ ...current, snapshot: next }));
    }).catch(reason => {
      if (!controller.signal.aborted) updatePage(workspaceRoot, current => ({ ...current, loadError: errorText(reason) }));
    }).finally(() => {
      if (!controller.signal.aborted) updatePage(workspaceRoot, current => ({ ...current, loading: false }));
    });
    return () => controller.abort();
  }, [active, workspaceRoot]);

  const save = (field: Field, requested: AgentConfigFields[Field] | null) => {
    const owner = workspaceRoot;
    const targetId = scope!.id;
    updatePage(owner, current => ({ ...current, pending: [...current.pending, field],
      errors: { ...current.errors, [field]: undefined }, overrides: { ...current.overrides, [field]: undefined } }));
    // Source uTn serializes writes and refreshes the chosen layer version at
    // dispatch. The queued edit retains its original workspace and scope.
    pendingConfigWrite = pendingConfigWrite.then(async () => {
      try {
        const current = await readConfig(owner);
        const target = current.scopes.find(item => item.id === targetId)!;
        const response = await fetchWithTimeout(new URL("/api/settings/agent-config", apiBase()), {
          method: "PUT", headers: authHeaders({ "content-type": "application/json" }),
          body: JSON.stringify({ workspace_root: owner, scope_id: targetId, expected_version: target.version, [field]: requested }),
        });
        if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
        const next = await response.json() as AgentConfigSnapshot;
        updatePage(owner, state => ({ ...state, snapshot: next, overrides: { ...state.overrides,
          [field]: next.write?.status === "overridden" ? next.write.fields[field] : undefined } }));
      } catch (reason) {
        updatePage(owner, current => ({ ...current, errors: { ...current.errors, [field]: errorText(reason) } }));
      } finally {
        updatePage(owner, current => ({ ...current, pending: current.pending.filter(item => item !== field) }));
      }
    });
    return pendingConfigWrite;
  };
  const openConfig = () => {
    const owner = workspaceRoot, target = scope!;
    updatePage(owner, current => ({ ...current, openError: null }));
    void openManagedSettingsTarget({ kind: "config", workspaceRoot: owner, scopeId: target.id })?.catch(reason => {
      updatePage(owner, current => ({ ...current, openError: { scopeId: target.id, message: errorText(reason) } }));
    });
  };
  const lockReason = (field: Field, hasOptions = true): string | undefined => {
    if (scope?.readonly || (scope && scope.path === null)) return t("此配置来源无法在此处编辑。");
    const origin = config?.origins[field];
    if (origin && managedSources.has(origin.source.type)) return t("此值由管理员策略管理。");
    if (!hasOptions) return t("此设置受到当前安装策略限制。");
    if (field === "approval_policy" && typeof value(field) === "object") return t("请在配置文件中编辑自定义审批策略。");
    return undefined;
  };
  const disabled = (field: Field, hasOptions = true) => !scope || page?.loading || page?.pending.includes(field) || Boolean(lockReason(field, hasOptions));
  const descriptions: Record<Field, string> = {
    approval_policy: t("选择 MiniCode 何时请求批准。"), sandbox_mode: t("选择 MiniCode 运行命令时的访问范围。"),
    network_access: t("允许工作区写入沙箱访问网络。"), web_search: t("选择 MiniCode 如何访问网页。"),
    model_verbosity: t("选择 MiniCode 在回复中包含多少细节。"), model_reasoning_summary: t("选择 MiniCode 如何概述推理过程。"),
  };
  const row = (field: Field, label: string, control: ReactNode, hasOptions = true) => {
    const lock = lockReason(field, hasOptions), origin = config?.origins[field];
    const error = scope?.kind === "user" ? page?.errors[field] : undefined;
    const overridden = scope?.kind === "user" ? page?.overrides[field] : undefined;
    return <div className="settings-row" key={field}><div className="settings-row-copy">
      <div className="settings-row-title">{label}</div>
      <div className="settings-row-description" id={`config-description-${field}`}>
        <div>{descriptions[field]}</div>
        {origin && <div className="config-field-origin">{t("生效来源：{source}", { source: origin.source.display })}</div>}
        {lock && <div className="config-field-lock"><span aria-hidden="true" className="config-lock-icon"
          style={{ mask: `url('${import.meta.env.BASE_URL}apps/config-lock.svg') center / contain no-repeat` }} />{lock}</div>}
        {error && <div className="config-field-error" role="alert">{error}</div>}
        {overridden && <div role="status">{overridden.overriding_source
          ? t("已保存，但当前生效值由{source}覆盖。", { source: overridden.overriding_source.source.display })
          : t("已保存，但当前生效值被更高优先级配置覆盖。")}</div>}
      </div>
    </div>{control}</div>;
  };
  const labels: Record<string, string> = { "on-request": t("按需"), never: t("从不"), untrusted: t("不受信任"), "on-failure": t("失败时"),
    "read-only": t("只读"), "workspace-write": t("工作区写入"), "danger-full-access": t("完整访问权限"),
    disabled: t("已禁用"), cached: t("缓存"), indexed: "Indexed", live: t("实时"),
    [modelDefault]: t("模型默认值"), low: t("低"), medium: t("中"), high: t("高"),
    auto: t("自动"), concise: t("简洁"), detailed: t("详细"), none: t("无") };
  const choice = (field: Exclude<Field, "network_access">, label: string, options: string[], optionDescriptions?: Record<string, string>) => {
    const current = value(field);
    const selected = current === null ? modelDefault : typeof current === "object" ? "granular" : current ?? "";
    const selectedLabel = !config ? t("正在加载…") : selected === "granular" ? t("自定义") : labels[selected] ?? selected;
    return row(field, label, <SelectMenu ariaLabel={label} ariaDescribedBy={`config-description-${field}`} className="settings-select"
      align="end" menuWidth={360} value={selected} selectedLabel={selectedLabel} disabled={disabled(field, options.length > 0)}
      optionDescriptions={optionDescriptions} onValueChange={next => { void save(field, next === modelDefault ? null : next); }}>
      {options.map(option => <option key={option} value={option}>{labels[option] ?? option}</option>)}
    </SelectMenu>, options.length > 0);
  };
  const approvalOptions = ["on-request", "never"].filter(option => !config?.requirements.allowed_approval_policies
    || config.requirements.allowed_approval_policies.includes(option));
  const sandboxOptions = ["read-only", "workspace-write", "danger-full-access"].filter(option => !config?.requirements.allowed_sandbox_modes
    || config.requirements.allowed_sandbox_modes.includes(option));
  const webOptions = ["disabled", "cached", "indexed", "live"].filter(option => !config?.requirements.allowed_web_search_modes
    || config.requirements.allowed_web_search_modes.includes(option));
  return <>
    <section className="settings-group">
      <div className="settings-section-heading settings-section-heading-row">
        <SelectMenu ariaLabel={t("配置作用域")} value={scopeId} selectedLabel={!config ? t("正在加载…") : undefined}
          className="settings-select config-scope-select" menuWidth={240} disabled={!config || config.scopes.length === 0}
          title={scope?.path ?? scope?.source.display} onValueChange={next => updatePage(workspaceRoot, current => ({ ...current, scopeId: next as "user" | "managed" }))}>
          {config?.scopes.map(item => <option key={item.id} value={item.id}>{item.kind === "managed" ? t("管理员") : t("用户")}</option>)}
        </SelectMenu>
        <button type="button" className="settings-action-button" disabled={!scope?.path} onClick={openConfig}>{t("打开配置文件")}</button>
      </div>
      <p className="settings-section-description">{t("为新聊天配置权限、网络访问和智能体回复。")}</p>
      {page?.loadError && <p role="alert">{page.loadError}</p>}
      {page?.openError?.scopeId === scopeId && <p role="alert">{page.openError.message}</p>}
      <div className="settings-card">
        {choice("approval_policy", t("审批策略"), approvalOptions, {
          "on-request": t("请求提升权限时询问批准"), never: t("受阻操作直接失败，不请求批准"),
        })}
        {choice("sandbox_mode", t("沙箱模式"), sandboxOptions, {
          "read-only": t("可以读取文件，但不能编辑"), "workspace-write": t("可以编辑文件，但仅限此工作区"),
          "danger-full-access": t("可以编辑此工作区之外的文件"),
        })}
        {value("sandbox_mode") === "workspace-write" && row("network_access", t("允许网络访问"),
          <button type="button" className="settings-toggle" role="switch" aria-label={t("允许网络访问")}
            aria-describedby="config-description-network_access" disabled={disabled("network_access")}
            aria-checked={value("network_access") ?? false} data-active={value("network_access") ?? false}
            onClick={() => { void save("network_access", !value("network_access")); }}><span /></button>)}
        {choice("web_search", t("网页搜索"), webOptions)}
        {choice("model_verbosity", t("回复详细程度"), [modelDefault, "low", "medium", "high"])}
        {choice("model_reasoning_summary", t("推理摘要"), ["auto", "concise", "detailed", "none"])}
      </div>
    </section>
    {children}
  </>;
}
