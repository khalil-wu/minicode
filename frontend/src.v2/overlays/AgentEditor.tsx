import { useCallback, useEffect, useMemo, useState } from "react";
import { Bot, LoaderCircle, Plus, Save, Trash2, X } from "lucide-react";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { useAppStore } from "../stores";
import {
  apiBase,
  authHeaders,
  errorMessageFromResponseText,
  fetchWithTimeout,
} from "../protocol/api";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { SelectMenu } from "../components/SelectMenu";

// Agent editor — CRUD over user-defined subagent roles (backed by
// /api/agents). Each agent is a markdown file (frontmatter + system-prompt
// body) that becomes a selectable subagent_type in the Task tool.

interface AgentRecord {
  name: string;
  description: string;
  prompt: string;
  model: string;
  effort: string;
  tools: string[];
  disallowed_tools: string[];
  source_path: string | null;
  filename: string;
  source: "project" | "user" | "policy" | "unknown";
  location: "project" | "user" | "policy" | "unknown";
  editable: boolean;
  deletable: boolean;
  can_override: boolean;
  active: boolean;
}

interface LlmSettingsSection {
  display_name?: string;
  model?: string;
  available_models?: string[];
  model_metadata?: Record<string, {
    reasoning_effort_levels?: string[];
    default_reasoning_effort?: string;
  }>;
  reasoning_effort_levels?: string[];
  default_reasoning_effort?: string;
  thinking_budget?: number;
}

interface LlmSettingsPayload {
  openai?: LlmSettingsSection;
  anthropic?: LlmSettingsSection;
  custom?: LlmSettingsSection;
}

interface AgentModelOption {
  value: string;
  label: string;
  provider: string;
  model: string;
  effortLevels: string[];
  defaultEffort: string;
}

interface AgentModelCatalogEntry {
  provider?: string;
  provider_name?: string;
  model?: string;
  model_name?: string;
  reasoning_effort_levels?: string[];
  default_reasoning_effort?: string;
}

// MiniCode's Agent ModelSelector exposes these canonical aliases. Exact
// provider/model entries from MiniCode's live ModelRuntime are listed after them.
const MINICODE_AGENT_MODEL_OPTIONS = [
  { value: "sonnet", label: "Sonnet（均衡）" },
  { value: "opus", label: "Opus（复杂推理）" },
  { value: "haiku", label: "Haiku（快速）" },
  { value: "inherit", label: "继承父会话" },
] as const;

const EMPTY_DRAFT: AgentRecord = {
  name: "",
  description: "",
  prompt: "",
  model: "",
  effort: "",
  tools: [],
  disallowed_tools: [],
  source_path: null,
  filename: "",
  source: "project",
  location: "project",
  editable: true,
  deletable: true,
  can_override: false,
  active: true,
};

const parseList = (value: string): string[] =>
  value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

const normalizeSource = (value: unknown): AgentRecord["source"] => {
  const source = String(value || "");
  if (source === "project") return "project";
  if (source === "user") return "user";
  if (source === "policy") return "policy";
  return "unknown";
};

const normalizeAgentRecord = (value: Partial<AgentRecord>): AgentRecord => {
  const source = normalizeSource(value.source || value.location);
  return {
    ...EMPTY_DRAFT,
    ...value,
    source,
    location: source,
    effort: String(value.effort || ""),
    filename: String(value.filename || value.name || ""),
    tools: Array.isArray(value.tools) ? value.tools : [],
    disallowed_tools: Array.isArray(value.disallowed_tools) ? value.disallowed_tools : [],
    editable: value.editable !== false,
    deletable: value.deletable !== false,
    can_override: value.can_override === true,
    active: value.active !== false,
  };
};

const agentSourceLabel = (source: AgentRecord["source"]): string => {
  if (source === "user") return "用户";
  if (source === "policy") return "托管";
  if (source === "project") return "项目";
  return "未知";
};

const agentSelectionKey = (agent: AgentRecord): string =>
  `${agent.source}:${agent.source_path || agent.filename || agent.name}`;

export const AgentEditor = () => {
  const open = useAppStore((s) => s.agentEditorOpen);
  const toggle = useAppStore((s) => s.toggleAgentEditor);
  const currentModel = useAppStore((s) => s.currentModel);
  const currentProvider = useAppStore((s) => s.currentProvider);
  const currentProviderId = useAppStore((s) => s.currentProviderId);
  const availableModels = useAppStore((s) => s.availableModels);
  const conversationId = useAppStore((s) => s.conversationId);
  const conversations = useAppStore((s) => s.conversations);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const runtimeCapabilities = useAppStore((s) => s.runtimeCapabilities);
  const [agents, setAgents] = useState<AgentRecord[]>([]);
  const [runtimeModelCatalog, setRuntimeModelCatalog] = useState<AgentModelCatalogEntry[]>([]);
  const [llmSettings, setLlmSettings] = useState<LlmSettingsPayload>({});
  const [draft, setDraft] = useState<AgentRecord>(EMPTY_DRAFT);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [deletingName, setDeletingName] = useState("");
  const dialogRef = useFocusTrap(open);
  const ownerWorkspaceRoot = useMemo(() => {
    const active = conversations.find((conversation) => conversation.id === conversationId);
    return String(active?.worktreePath || active?.workspaceRoot || workingDirectory || "").trim();
  }, [conversationId, conversations, workingDirectory]);
  const ownerQuery = useMemo(() => {
    const query = new URLSearchParams();
    if (ownerWorkspaceRoot) query.set("workspace_root", ownerWorkspaceRoot);
    if (conversationId) query.set("conversation_id", conversationId);
    return query.toString();
  }, [conversationId, ownerWorkspaceRoot]);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const [res, settingsRes] = await Promise.all([
        fetchWithTimeout(`${apiBase()}/api/agents${ownerQuery ? `?${ownerQuery}` : ""}`, {
          cache: "no-store",
          headers: authHeaders(),
        }, { timeoutMessage: "加载 Agent 列表超时，请重试。" }),
        fetchWithTimeout(`${apiBase()}/api/llm/settings`, {
          cache: "no-store",
          headers: authHeaders(),
        }, { timeoutMessage: "加载模型列表超时，请重试。" }).catch(() => null),
      ]);
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(errorMessageFromResponseText(text, res.statusText || `HTTP ${res.status}`));
      }
      const data = await res.json();
      setAgents(Array.isArray(data.agents) ? data.agents.map(normalizeAgentRecord) : []);
      setRuntimeModelCatalog(Array.isArray(data.model_catalog) ? data.model_catalog : []);
      if (settingsRes?.ok) {
        const settings = await settingsRes.json().catch(() => ({}));
        setLlmSettings(settings && typeof settings === "object" ? settings : {});
      }
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err || "未知错误");
      setLoadError(detail);
      pushToast(`加载 Agent 失败：${detail}`, "error");
    } finally {
      setLoading(false);
    }
  }, [ownerQuery]);

  const modelOptions = useMemo<AgentModelOption[]>(() => {
    const options: AgentModelOption[] = [];
    const seen = new Set<string>();
    const add = (option: AgentModelOption) => {
      if (!option.provider || !option.model || seen.has(option.value)) return;
      seen.add(option.value);
      options.push(option);
    };
    for (const entry of runtimeModelCatalog) {
      const provider = String(entry.provider || "").trim();
      const model = String(entry.model || "").trim();
      const effortLevels = Array.from(new Set(
        (Array.isArray(entry.reasoning_effort_levels) ? entry.reasoning_effort_levels : [])
          .map((value) => String(value || "").trim().toLowerCase())
          .filter(Boolean),
      ));
      add({
        value: `${provider}/${model}`,
        label: `${entry.provider_name || provider} · ${entry.model_name || model}`,
        provider,
        model,
        effortLevels,
        defaultEffort: String(entry.default_reasoning_effort || "").trim().toLowerCase(),
      });
    }
    for (const provider of ["openai", "anthropic", "custom"] as const) {
      const section = llmSettings[provider];
      if (!section) continue;
      const models = Array.from(new Set([
        section.model,
        ...(Array.isArray(section.available_models) ? section.available_models : []),
      ].map((value) => String(value || "").trim()).filter(Boolean)));
      for (const model of models) {
        const metadata = section.model_metadata?.[model] || {};
        const effortLevels = Array.from(new Set(
          (metadata.reasoning_effort_levels
            || (model === section.model ? section.reasoning_effort_levels : [])
            || (provider === "anthropic" && Number(section.thinking_budget || 0) > 0 ? ["off", "high"] : []))
            .map((value) => String(value || "").trim().toLowerCase())
            .filter(Boolean),
        ));
        add({
          value: `${provider}/${model}`,
          label: `${section.display_name || provider} · ${model}`,
          provider,
          model,
          effortLevels,
          defaultEffort: String(metadata.default_reasoning_effort || section.default_reasoning_effort || "").trim().toLowerCase(),
        });
      }
    }

    const liveProvider = String(currentProviderId || currentProvider || "").trim();
    const capabilities = runtimeCapabilities?.provider_capabilities;
    const liveEfforts = Array.isArray(capabilities?.reasoning_effort_levels)
      ? Array.from(new Set(capabilities.reasoning_effort_levels
        .map((value) => String(value || "").trim().toLowerCase())
        .filter(Boolean)))
      : [];
    for (const model of Array.from(new Set([currentModel, ...availableModels].map((value) => String(value || "").trim()).filter(Boolean)))) {
      add({
        value: `${liveProvider}/${model}`,
        label: `${currentProvider || liveProvider} · ${model}`,
        provider: liveProvider,
        model,
        effortLevels: model === currentModel ? liveEfforts : [],
        defaultEffort: model === currentModel
          ? String(capabilities?.default_reasoning_effort || "").trim().toLowerCase()
          : "",
      });
    }
    return options;
  }, [availableModels, currentModel, currentProvider, currentProviderId, llmSettings, runtimeCapabilities, runtimeModelCatalog]);

  const selectedModelOption = modelOptions.find((option) => option.value === draft.model)
    ?? modelOptions.find((option) => !draft.model.includes("/") && option.model === draft.model);
  const effortOptions = Array.from(new Set([
    ...(selectedModelOption?.effortLevels || []),
    ...(draft.effort ? [draft.effort] : []),
  ]));

  useEffect(() => {
    if (open) {
      void load();
      setDraft(EMPTY_DRAFT);
    }
  }, [open, load]);

  if (!open) return null;

  const editExisting = (a: AgentRecord) => setDraft({ ...a, location: a.source });
  const newDraft = () => setDraft(EMPTY_DRAFT);
  const readOnlyDraft = Boolean(draft.source_path && !draft.editable);
  const selectedDraftKey = agentSelectionKey(draft);

  const save = async () => {
    const name = draft.name.trim();
    if (!name) {
      pushToast("请输入 Agent 名称", "warning");
      return;
    }
    if (readOnlyDraft) {
      pushToast("该 Agent 来自只读托管作用域，不能修改源文件。", "info");
      return;
    }
    setSaving(true);
    try {
      const res = await fetchWithTimeout(`${apiBase()}/api/agents`, {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({
          name,
          description: draft.description,
          prompt: draft.prompt,
          model: draft.model,
          effort: draft.effort,
          tools: draft.tools,
          disallowed_tools: draft.disallowed_tools,
          source: draft.source_path ? draft.source : "",
          location: draft.source_path ? "" : draft.location,
          source_path: draft.source_path || "",
          workspace_root: ownerWorkspaceRoot,
        }),
      }, { timeoutMessage: "保存 Agent 超时，请重试。" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(errorMessageFromResponseText(text, res.statusText || `HTTP ${res.status}`));
      }
      const data = await res.json().catch(() => ({}));
      pushToast(`已保存 Agent“${name}”`, "success");
      await load();
      if (data?.agent) setDraft(normalizeAgentRecord(data.agent));
    } catch (err) {
      pushToast(`保存 Agent 失败：${err instanceof Error ? err.message : err}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (agent: AgentRecord) => {
    if (deletingName) return;
    if (!agent.deletable) {
      pushToast("该 Agent 来自只读作用域，不能删除源文件。", "info");
      return;
    }
    const name = agent.name;
    const confirmed = await showConfirm({
      title: "删除 Agent",
      message: `确定删除“${name}”吗？此操作无法撤销。`,
      confirmLabel: "删除",
      cancelLabel: "取消",
      danger: true,
    });
    if (!confirmed) return;
    setDeletingName(name);
    try {
      const query = new URLSearchParams({
        source: agent.source,
        source_path: agent.source_path || "",
      });
      if (ownerWorkspaceRoot) query.set("workspace_root", ownerWorkspaceRoot);
      if (conversationId) query.set("conversation_id", conversationId);
      const res = await fetchWithTimeout(`${apiBase()}/api/agents/${encodeURIComponent(name)}?${query.toString()}`, {
        method: "DELETE",
        headers: authHeaders(),
      }, { timeoutMessage: "删除 Agent 超时，请重试。" });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(errorMessageFromResponseText(text, res.statusText || `HTTP ${res.status}`));
      }
      pushToast(`已删除 Agent“${name}”`, "success");
      if (selectedDraftKey === agentSelectionKey(agent)) setDraft(EMPTY_DRAFT);
      await load();
    } catch (err) {
      pushToast(`删除 Agent 失败：${err instanceof Error ? err.message : err}`, "error");
    } finally {
      setDeletingName("");
    }
  };

  const inputStyle: React.CSSProperties = {
    width: "100%",
    padding: "8px 10px",
    borderRadius: "var(--radius-md)",
    border: "1px solid var(--border-subtle)",
    background: "var(--surface-base)",
    color: "var(--text-primary)",
    fontSize: "var(--mc-font-body)",
    boxSizing: "border-box",
  };
  const labelStyle: React.CSSProperties = {
    fontSize: "var(--mc-font-caption)",
    color: "var(--text-secondary)",
    marginBottom: 4,
    display: "block",
  };

  return (
    <div
      className="overlay-backdrop"
      onClick={() => toggle()}
      style={{
        position: "fixed",
        inset: 0,
        background: "var(--backdrop-overlay)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: "var(--z-modal)",
        pointerEvents: "auto",
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Agent 编辑器"
        tabIndex={-1}
        className="modal-content agent-editor"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            toggle();
          }
        }}
        style={{
          width: "min(860px, 100%)",
          height: "min(680px, 90vh)",
          background: "var(--surface-raised)",
          border: "1px solid var(--border-subtle)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--shadow-strong-overlay)",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          className="agent-editor-header"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "14px 16px",
            borderBottom: "1px solid var(--border-subtle)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Bot size={16} />
            <h2 style={{ margin: 0, fontSize: "var(--mc-font-heading)", color: "var(--text-primary)" }}>Agent 编辑器</h2>
          </div>
          <button
            type="button"
            onClick={() => toggle()}
            aria-label="关闭 Agent 编辑器"
            className="mc-icon-button agent-editor-close"
          >
            <X size={16} />
          </button>
        </div>

        <div className="agent-editor-body" style={{ display: "flex", flex: 1, minHeight: 0 }}>
          {/* Left: agent list */}
          <div
            className="agent-editor-sidebar"
            style={{
              width: 240,
              borderRight: "1px solid var(--border-subtle)",
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
            }}
          >
            <button
              type="button"
              onClick={newDraft}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                margin: "10px",
                padding: "8px 10px",
                borderRadius: "var(--radius-md)",
                border: "1px dashed var(--border-subtle)",
                background: "transparent",
                color: "var(--text-primary)",
                cursor: "pointer",
                fontSize: "var(--mc-font-body)",
              }}
            >
              <Plus size={14} /> 新建 Agent
            </button>
            <div style={{ overflowY: "auto", flex: 1, padding: "0 10px 10px" }}>
              {loading && <div style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)", padding: 8 }}>正在加载…</div>}
              {!loading && loadError && (
                <div role="alert" style={{ color: "var(--state-danger)", fontSize: "var(--mc-font-secondary)", padding: 8 }}>
                  <div style={{ marginBottom: 8 }}>加载失败：{loadError}</div>
                  <button
                    type="button"
                    className="settings-action-button"
                    onClick={() => void load()}
                    disabled={loading}
                  >
                    重试
                  </button>
                </div>
              )}
              {!loading && !loadError && agents.length === 0 && (
                <div style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)", padding: 8 }}>暂无自定义 Agent</div>
              )}
              {agents.map((a) => (
                <div
                  key={agentSelectionKey(a)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 2,
                    borderRadius: "var(--radius-md)",
                    background: selectedDraftKey === agentSelectionKey(a) ? "var(--surface-active)" : "transparent",
                  }}
                >
                  <button
                    type="button"
                    aria-pressed={selectedDraftKey === agentSelectionKey(a)}
                    onClick={() => editExisting(a)}
                    style={{
                      flex: 1,
                      minWidth: 0,
                      alignSelf: "stretch",
                      padding: "8px 6px 8px 10px",
                      border: 0,
                      background: "transparent",
                      cursor: "pointer",
                      textAlign: "left",
                    }}
                  >
                    <div style={{ minWidth: 0 }}>
                      <div
                        style={{
                          fontSize: "var(--mc-font-body)",
                          color: "var(--text-primary)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {a.name}
                      </div>
                      {a.description && (
                        <div
                          style={{
                            fontSize: "var(--mc-font-secondary)",
                            color: "var(--text-muted)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {a.description}
                        </div>
                      )}
                      <div style={{ fontSize: "var(--mc-font-secondary)", color: "var(--text-muted)", marginTop: 2 }}>
                        {agentSourceLabel(a.source)}
                        {a.active ? " · 生效中" : " · 已被覆盖"}
                        {!a.editable ? " · 只读" : ""}
                      </div>
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      void remove(a);
                    }}
                    aria-label={`删除 Agent ${a.name}`}
                    title={a.deletable ? `删除 ${a.name}` : "只读来源不能删除"}
                    className="mc-icon-button mc-icon-button-compact mc-icon-button-danger"
                    disabled={!a.deletable || Boolean(deletingName) || saving}
                    style={{ flexShrink: 0, marginRight: 6, opacity: deletingName === a.name ? 0.55 : 1 }}
                  >
                    {deletingName === a.name ? <LoaderCircle size={14} className="animate-spin" aria-hidden="true" /> : <Trash2 size={14} />}
                  </button>
                </div>
              ))}
            </div>
          </div>

          {/* Right: editor form */}
          <div className="agent-editor-form" style={{ flex: 1, overflowY: "auto", padding: "16px", display: "flex", flexDirection: "column", gap: 12 }}>
            <div>
              <label htmlFor="agent-editor-location" style={labelStyle}>位置</label>
              <SelectMenu
                id="agent-editor-location"
                ariaLabel="位置"
                value={draft.source_path ? draft.source : draft.location}
                disabled={Boolean(draft.source_path)}
                onValueChange={(value) => {
                  const location = normalizeSource(value);
                  if (location !== "project" && location !== "user") return;
                  setDraft((current) => ({ ...current, source: location, location }));
                }}
              >
                <option value="project">项目（.minicode/agents）</option>
                <option value="user">用户（~/.minicode/agents）</option>
                {draft.source === "policy" && <option value="policy">托管（只读）</option>}
                {draft.source === "unknown" && <option value="unknown">未知来源（只读）</option>}
              </SelectMenu>
              <div style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)", marginTop: 5 }}>
                {draft.source_path
                  ? draft.source_path
                  : draft.location === "user"
                    ? "新 Agent 将写入 ~/.minicode/agents 目录。"
                    : "新 Agent 将写入当前项目的 .minicode/agents 目录。"}
              </div>
            </div>
            <div>
              <label htmlFor="agent-editor-name" style={labelStyle}>名称</label>
              <input
                id="agent-editor-name"
                style={inputStyle}
                value={draft.name}
                disabled={Boolean(draft.source_path)}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
                placeholder="reviewer"
              />
            </div>
            <div>
              <label htmlFor="agent-editor-description" style={labelStyle}>说明</label>
              <input
                id="agent-editor-description"
                style={inputStyle}
                value={draft.description}
                disabled={readOnlyDraft}
                onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
                placeholder="说明这个 Agent 最适合处理的任务"
              />
            </div>
            <div style={{ display: "flex", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <label htmlFor="agent-editor-model" style={labelStyle}>模型</label>
                <SelectMenu
                  id="agent-editor-model"
                  ariaLabel="模型"
                  value={draft.model}
                  disabled={readOnlyDraft}
                  onValueChange={(model) => {
                    const next = modelOptions.find((option) => option.value === model);
                    setDraft((current) => ({
                      ...current,
                      model,
                      effort: current.effort && next?.effortLevels.includes(current.effort)
                        ? current.effort
                        : "",
                    }));
                  }}
                  ariaDescribedBy="agent-model-help"
                >
                  <option value="">省略模型（继承父会话）</option>
                  {draft.model
                    && !modelOptions.some((option) => option.value === draft.model)
                    && !MINICODE_AGENT_MODEL_OPTIONS.some((option) => option.value === draft.model)
                    && (
                      <option value={draft.model}>{draft.model}（现有定义）</option>
                    )}
                  <optgroup label="MiniCode 模型别名">
                    {MINICODE_AGENT_MODEL_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </optgroup>
                  {modelOptions.length > 0 && (
                    <optgroup label="当前运行时模型">
                      {modelOptions.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </optgroup>
                  )}
                </SelectMenu>
                <div id="agent-model-help" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)", marginTop: 5 }}>
                  省略时继承父会话模型与推理强度；选择具体模型时使用 Provider/Model 精确绑定。
                </div>
              </div>
              <div style={{ flex: 1 }}>
                <label htmlFor="agent-editor-effort" style={labelStyle}>推理强度</label>
                <SelectMenu
                  id="agent-editor-effort"
                  ariaLabel="推理强度"
                  value={draft.effort}
                  disabled={readOnlyDraft}
                  onValueChange={(value) => setDraft((current) => ({ ...current, effort: value }))}
                  ariaDescribedBy="agent-effort-help"
                >
                  <option value="">
                    {draft.model ? "使用目标模型默认值" : "继承父会话"}
                  </option>
                  {effortOptions.map((effort) => (
                    <option key={effort} value={effort}>{effort}</option>
                  ))}
                </SelectMenu>
                <div id="agent-effort-help" style={{ color: "var(--text-muted)", fontSize: "var(--mc-font-secondary)", marginTop: 5 }}>
                  {selectedModelOption?.defaultEffort
                    ? `目标模型默认：${selectedModelOption.defaultEffort}`
                    : effortOptions.length
                      ? "仅显示该模型声明支持的值。"
                      : "该模型未声明可选强度；运行时会使用模型默认值。"}
                </div>
              </div>
            </div>
            <div>
              <label htmlFor="agent-editor-tools" style={labelStyle}>允许的工具</label>
              <input
                id="agent-editor-tools"
                style={inputStyle}
                value={draft.tools.join(", ")}
                disabled={readOnlyDraft}
                onChange={(e) => setDraft((d) => ({ ...d, tools: parseList(e.target.value) }))}
                placeholder="read_file, grep_files, run_command"
              />
            </div>
            <div>
              <label htmlFor="agent-editor-disallowed-tools" style={labelStyle}>禁止的工具</label>
              <input
                id="agent-editor-disallowed-tools"
                style={inputStyle}
                value={draft.disallowed_tools.join(", ")}
                disabled={readOnlyDraft}
                onChange={(e) => setDraft((d) => ({ ...d, disallowed_tools: parseList(e.target.value) }))}
                placeholder="write_file"
              />
            </div>
            <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 160 }}>
              <label htmlFor="agent-editor-prompt" style={labelStyle}>系统提示词</label>
              <textarea
                id="agent-editor-prompt"
                value={draft.prompt}
                disabled={readOnlyDraft}
                onChange={(e) => setDraft((d) => ({ ...d, prompt: e.target.value }))}
                placeholder="你是一名专注、严谨的代码审查 Agent…"
                style={{
                  ...inputStyle,
                  flex: 1,
                  minHeight: 140,
                  resize: "vertical",
                  fontFamily: "var(--font-mono, monospace)",
                  lineHeight: 1.5,
                }}
              />
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button
                type="button"
                onClick={save}
                disabled={readOnlyDraft || saving || Boolean(deletingName)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  padding: "9px 16px",
                  borderRadius: "var(--radius-md)",
                  border: 0,
                  background: "var(--text-primary)",
                  color: "var(--surface-base)",
                  cursor: readOnlyDraft || saving || deletingName ? "default" : "pointer",
                  fontSize: "var(--mc-font-body)",
                  fontWeight: "var(--fw-semibold)",
                  opacity: readOnlyDraft || saving || deletingName ? 0.6 : 1,
                }}
              >
                <Save size={14} /> {readOnlyDraft
                  ? "只读来源"
                  : saving
                  ? "正在保存…"
                  : "保存"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
