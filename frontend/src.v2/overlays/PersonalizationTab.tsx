import { useI18n, type Translate } from "../i18n";
import { useCallback, useRef, useState, type KeyboardEvent } from "react";
import { ArrowLeft, ChevronRight, LoaderCircle, RotateCcw, ShieldAlert } from "lucide-react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { pushToast } from "./ToastContainer";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { Section } from "./settingsShared";
import { showConfirm } from "./DialogService";
import "./PersonalizationTab.css";

type PersonalizationPayload = {
  instructions: string;
  path: string;
  exists: boolean;
  max_bytes: number;
};

type MemoryRequest = { action: "mode" | "restore"; pending: boolean; error: string };

const fileName = (path: string): string => path.split(/[\\/]/).pop()!;
const errorMessage = (error: unknown): string => error instanceof Error ? error.message : String(error);

const pollutionSourceLabel = (tr: Translate, source: string): string => {
  const normalized = source.trim().toLowerCase();
  if (normalized === "web_search" || normalized.includes("web_search")) return tr("联网搜索");
  if (normalized === "web_fetch" || normalized.includes("web_fetch")) return tr("网页读取");
  if (normalized === "tool_search") return tr("工具检索");
  if (normalized === "browser_control") return tr("浏览器");
  if (normalized.startsWith("mcp__") || normalized.includes("mcp_")) return "MCP";
  return source;
};

export const PersonalizationTab = ({ active = true }: { active?: boolean }) => {
  const { t: tr } = useI18n();
  const conversationId = useAppStore((state) => state.conversationId);
  const currentConversation = useAppStore((state) => state.conversations.find((conversation) => conversation.id === state.conversationId));
  const memoryPolluted = currentConversation?.memoryPolluted === true;
  const memoryMode = memoryPolluted ? "polluted" : currentConversation?.memoryMode || "enabled";
  const memoryPollutionSources = currentConversation?.memoryPollutionSources || [];
  const [payload, setPayload] = useState<PersonalizationPayload | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [editingInstructions, setEditingInstructions] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [memoryRequests, setMemoryRequests] = useState<Record<string, MemoryRequest>>({});
  const [resettingMemory, setResettingMemory] = useState(false);
  const [resetError, setResetError] = useState("");
  const loadEpoch = useRef(0);
  const instructionEndpoint = useRef(`${apiBase()}/api/settings/personalization`).current;
  const contents = draft ?? payload?.instructions ?? "";
  const dirty = payload !== null && draft !== null && draft !== payload.instructions;
  const tooLarge = payload !== null && new TextEncoder().encode(contents).length > payload.max_bytes;
  const memoryRequest = conversationId ? memoryRequests[conversationId] : undefined;
  const savingMemoryMode = memoryRequest?.pending === true && memoryRequest.action === "mode";
  const clearingPollution = memoryRequest?.pending === true && memoryRequest.action === "restore";

  const load = useCallback(async () => {
    const epoch = ++loadEpoch.current;
    setLoading(true);
    setLoadError("");
    try {
      const response = await fetchWithTimeout(instructionEndpoint, { cache: "no-store", headers: authHeaders() });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const next = await response.json() as PersonalizationPayload;
      if (epoch === loadEpoch.current) setPayload(next);
    } catch (error) {
      if (epoch === loadEpoch.current) setLoadError(errorMessage(error));
    } finally {
      if (epoch === loadEpoch.current) setLoading(false);
    }
  }, [instructionEndpoint]);

  const openInstructions = () => {
    setEditingInstructions(true);
    setSaveError("");
    void load();
  };

  const closeInstructions = () => {
    setDraft(null);
    setSaveError("");
    setEditingInstructions(false);
  };

  const backToPersonalization = async () => {
    if (saving) return;
    if (dirty && !await showConfirm({
      title: tr("放弃未保存的更改？"),
      message: tr("返回后将放弃对自定义指令的更改。"),
      confirmLabel: tr("放弃更改"),
      cancelLabel: tr("继续编辑"),
    })) return;
    closeInstructions();
  };

  const save = async () => {
    if (!dirty || tooLarge || saving) return;
    const submitted = contents;
    // A read started before this explicit save must not replace its result.
    loadEpoch.current += 1;
    setLoading(false);
    setSaving(true);
    setSaveError("");
    try {
      const response = await fetchWithTimeout(instructionEndpoint, {
        method: "PUT",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ instructions: submitted }),
      }, { timeoutMessage: tr("保存自定义指令超时，请重试。") });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const next = await response.json() as PersonalizationPayload;
      setPayload(next);
      setDraft(null);
      setEditingInstructions(false);
      pushToast(tr("自定义指令已保存"), "success");
    } catch (error) {
      const message = errorMessage(error);
      setSaveError(message);
      pushToast(tr("自定义指令保存失败：{v0}", { v0: message }), "error");
    } finally {
      setSaving(false);
    }
  };

  const onEditorKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!active || event.nativeEvent.isComposing || !(event.metaKey || event.ctrlKey)
      || !["s", "Enter"].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    void save();
  };

  const isCurrentMemoryOwner = (owner: string) => useAppStore.getState().conversationId === owner;

  const updateMemoryMode = async (enabled: boolean) => {
    if (!conversationId || memoryRequest?.pending || memoryPolluted) return;
    const owner = conversationId;
    const nextMode = enabled ? "enabled" : "disabled";
    if (nextMode === memoryMode) return;
    setMemoryRequests((current) => ({ ...current, [owner]: { action: "mode", pending: true, error: "" } }));
    let failure = "";
    try {
      const result = await sendClientCommandAwaitResult({
        type: "conversation.memory_mode.set", conversation_id: owner, memory_mode: nextMode,
      }, "conversation.memory_mode.set");
      if (!commandResultSucceeded(result)) throw new Error(result.message);
      if (isCurrentMemoryOwner(owner)) pushToast(enabled ? tr("当前任务已启用长期记忆。") : tr("当前任务已关闭长期记忆。"), "success");
    } catch (error) {
      failure = errorMessage(error);
      if (isCurrentMemoryOwner(owner)) pushToast(tr("长期记忆设置失败：{v0}", { v0: failure }), "error");
    } finally {
      setMemoryRequests((current) => ({ ...current, [owner]: { action: "mode", pending: false, error: failure } }));
    }
  };

  const resetMemory = async () => {
    if (resettingMemory) return;
    const confirmed = await showConfirm({
      title: tr("清除长期记忆"),
      message: tr("这会清除所有生成的长期记忆和派生摘要。任务对话、目标以及每个任务选择的记忆模式会保留。"),
      confirmLabel: tr("清除记忆"), danger: true,
    });
    if (!confirmed) return;
    setResettingMemory(true);
    setResetError("");
    try {
      const result = await sendClientCommandAwaitResult({ type: "memory.reset", confirmed: true }, "memory.reset");
      if (!commandResultSucceeded(result)) throw new Error(result.message);
      pushToast(tr("长期记忆已清除，任务对话和记忆模式已保留。"), "success");
    } catch (error) {
      const message = errorMessage(error);
      setResetError(message);
      pushToast(tr("记忆清除失败：{v0}", { v0: message }), "error");
    } finally {
      setResettingMemory(false);
    }
  };

  const clearPollution = async () => {
    if (!conversationId || memoryRequest?.pending) return;
    const owner = conversationId;
    setMemoryRequests((current) => ({ ...current, [owner]: { action: "restore", pending: true, error: "" } }));
    let failure = "";
    try {
      const confirmed = await showConfirm({
        title: tr("重新启用任务记忆"),
        message: tr("此任务包含联网、浏览器或 MCP 外部上下文。重新启用后，后续回复可以再次生成长期记忆。"),
        confirmLabel: tr("重新启用"),
      });
      if (!confirmed || !isCurrentMemoryOwner(owner)) return;
      const result = await sendClientCommandAwaitResult({
        type: "conversation.memory_mode.set", conversation_id: owner, memory_mode: "enabled",
      }, "conversation.memory_mode.set");
      if (!commandResultSucceeded(result)) throw new Error(result.message);
      if (isCurrentMemoryOwner(owner)) pushToast(tr("当前任务已重新启用长期记忆。"), "success");
    } catch (error) {
      failure = errorMessage(error);
      if (isCurrentMemoryOwner(owner)) pushToast(tr("任务记忆恢复失败：{v0}", { v0: failure }), "error");
    } finally {
      setMemoryRequests((current) => ({ ...current, [owner]: { action: "restore", pending: false, error: failure } }));
    }
  };

  if (editingInstructions) return (
    <section className="personalization-instructions-page" aria-labelledby="personalization-instructions-title" onKeyDown={onEditorKeyDown}>
      <header className="settings-page-heading personalization-editor-heading">
        <button type="button" className="personalization-back" onClick={() => void backToPersonalization()} disabled={saving}>
          <ArrowLeft aria-hidden="true" />{tr("返回")}
        </button>
        <div className="personalization-editor-actions">
          <button type="button" className="settings-action-button" onClick={closeInstructions} disabled={saving}>{tr("取消")}</button>
          <button type="button" className="settings-action-button" data-primary="true" onClick={() => void save()} disabled={!dirty || tooLarge || saving}>
            {saving && <LoaderCircle className="settings-spin" aria-hidden="true" />}{saving ? tr("保存中") : tr("保存")}
          </button>
        </div>
        <h2 id="personalization-instructions-title">{tr("自定义指令")}</h2>
        <p>{payload ? tr("编辑此电脑上的 {v0}。项目指令也可能适用。", { v0: fileName(payload.path) }) : tr("编辑此电脑上的自定义指令，项目指令也可能适用。")}</p>
      </header>
      {loadError && <div className="personalization-editor-error" role="alert">
        <span>{tr("自定义指令暂时不可用")}：{loadError}</span>
        <button type="button" className="settings-action-button" onClick={() => void load()} disabled={loading || saving}>{tr("重试")}</button>
      </div>}
      {loading && !payload ? <div className="personalization-loading" role="status"><LoaderCircle className="settings-spin" aria-hidden="true" />{tr("正在读取指令…")}</div>
        : payload && <textarea className="personalization-instructions-input" aria-label={tr("自定义指令")} value={contents}
          onChange={(event) => { setDraft(event.target.value); setSaveError(""); }} rows={13} spellCheck={false} disabled={saving}
          placeholder={tr("添加自定义指令…")} autoFocus />}
      {tooLarge && payload && <p className="personalization-inline-error" role="alert">{tr("自定义指令不能超过 {v0} KiB", { v0: payload.max_bytes / 1024 })}</p>}
      {saveError && <p className="personalization-inline-error" role="alert">{tr("自定义指令保存失败：{v0}", { v0: saveError })}</p>}
    </section>
  );

  return <div className="personalization-tab">
    <Section title={tr("当前任务记忆")} description={tr("控制当前任务是否参与长期记忆生成。")}>
      <div className="personalization-memory-card">
        <div className="personalization-memory-row">
          <div className="settings-row-copy">
            <div className="settings-row-title">{tr("长期记忆生成")}</div>
            <div className="settings-row-description">{tr("从当前任务生成可复用的记忆，供今后的任务参考。")}</div>
          </div>
          <button type="button" className="settings-toggle" role="switch" aria-label={tr("长期记忆生成")}
            aria-checked={memoryMode === "enabled"} data-active={memoryMode === "enabled" ? "true" : "false"}
            disabled={!conversationId || memoryPolluted || savingMemoryMode || clearingPollution}
            onClick={() => void updateMemoryMode(memoryMode !== "enabled")}><span /></button>
        </div>
        <div className="personalization-memory-row">
          <div className="settings-row-copy">
            <div className="settings-row-title">{tr("清除长期记忆")}</div>
            <div className="settings-row-description">{tr("删除此电脑上生成的长期记忆，保留任务对话与记忆模式。")}</div>
          </div>
          <button type="button" className="settings-action-button" data-danger="true" disabled={resettingMemory} onClick={() => void resetMemory()}>
            {resettingMemory && <LoaderCircle className="settings-spin" aria-hidden="true" />}{resettingMemory ? tr("清除中") : tr("清除记忆")}
          </button>
        </div>
      </div>
      {memoryPolluted && <div className="personalization-memory-isolation" role="status">
        <ShieldAlert aria-hidden="true" />
        <div className="personalization-isolation-copy">
          <strong>{tr("外部上下文已隔离")}</strong>
          <span>{tr("此任务不会生成或传递新的长期记忆。")}</span>
          {memoryPollutionSources.length > 0 && <span className="personalization-isolation-sources">
            {Array.from(new Set(memoryPollutionSources)).map((source) => <span key={source}>
              <span>{pollutionSourceLabel(tr, source)}</span>
              {pollutionSourceLabel(tr, source) !== source && <span className="personalization-source-detail">{source}</span>}
            </span>)}
          </span>}
        </div>
        <button type="button" className="settings-action-button" disabled={clearingPollution || savingMemoryMode} onClick={() => void clearPollution()}>
          {clearingPollution ? <LoaderCircle className="settings-spin" aria-hidden="true" /> : <RotateCcw aria-hidden="true" />}
          {clearingPollution ? tr("恢复中") : tr("重新启用")}
        </button>
      </div>}
      {memoryRequest?.error && <p className="personalization-inline-error" role="alert">{memoryRequest.error}</p>}
      {resetError && <p className="personalization-inline-error" role="alert">{tr("记忆清除失败：{v0}", { v0: resetError })}</p>}
      {!conversationId && <p className="settings-page-note">{tr("新建任务后可设置记忆。")}</p>}
    </Section>
    <Section title={tr("自定义指令")}>
      <button type="button" className="personalization-instructions-entry" aria-label={tr("编辑本机指令")} onClick={openInstructions}>
        <span className="settings-row-copy">
          <span className="settings-row-title">{tr("本机指令")}</span>
          <span className="settings-row-description">{tr("编辑此电脑上的自定义指令，项目指令也可能适用。")}</span>
        </span>
        <ChevronRight aria-hidden="true" />
      </button>
    </Section>
  </div>;
};
