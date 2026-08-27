import { useCallback, useEffect, useMemo, useState } from "react";
import { Brain, Check, FileText, Info, LoaderCircle, RotateCcw, Save, ServerOff, ShieldAlert, Trash2 } from "lucide-react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { pushToast } from "./ToastContainer";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { Section } from "./settingsShared";
import { fetchJsonWithStartupRetry, formatSettingsLoadError } from "./settingsLoad";
import { showConfirm } from "./DialogService";

type PersonalizationPayload = {
  instructions: string;
  path: string;
  exists: boolean;
  max_bytes: number;
};

type GuidelineSource = {
  path: string;
  scope: string;
  source_kind: string;
  label: string;
};

type GuidelinePayload = {
  blocks?: GuidelineSource[];
};

const sourceLabel = (source: GuidelineSource): string => {
  if (source.source_kind === "user_memory") return "全局指令";
  if (source.source_kind === "agent_instruction") return "项目指令";
  if (source.source_kind.includes("rule")) return "规则";
  if (source.source_kind.includes("memory")) return "记忆";
  return source.label || "指令";
};

const fileName = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() || path;

const pollutionSourceLabel = (source: string): string => {
  const normalized = source.trim().toLowerCase();
  if (normalized === "web_search" || normalized.includes("web_search")) return "联网搜索";
  if (normalized === "web_fetch" || normalized.includes("web_fetch")) return "网页读取";
  if (normalized === "tool_search") return "工具检索";
  if (normalized === "browser_control") return "浏览器";
  if (normalized.startsWith("mcp__") || normalized.includes("mcp_")) return "MCP";
  return source;
};

export const PersonalizationTab = () => {
  const conversationId = useAppStore((state) => state.conversationId);
  const currentConversation = useAppStore((state) => state.conversations.find((conversation) => conversation.id === state.conversationId));
  const memoryPolluted = currentConversation?.memoryPolluted === true;
  const memoryMode = memoryPolluted
    ? "polluted"
    : currentConversation?.memoryMode || "enabled";
  const memoryPollutionSources = currentConversation?.memoryPollutionSources || [];
  const [payload, setPayload] = useState<PersonalizationPayload | null>(null);
  const [draft, setDraft] = useState("");
  const [sources, setSources] = useState<GuidelineSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingMemoryMode, setSavingMemoryMode] = useState(false);
  const [resettingMemory, setResettingMemory] = useState(false);
  const [clearingPollution, setClearingPollution] = useState(false);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const [personalization, guidelines] = await Promise.all([
        fetchJsonWithStartupRetry<PersonalizationPayload>(`${apiBase()}/api/settings/personalization`, {
          cache: "no-store",
          headers: authHeaders(),
        }, { cacheKey: "settings.personalization" }),
        fetchJsonWithStartupRetry<GuidelinePayload>(`${apiBase()}/api/guidelines`, {
          cache: "no-store",
          headers: authHeaders(),
        }, { cacheKey: "settings.guidelines" }).catch(() => ({ blocks: [] })),
      ]);
      setPayload(personalization);
      setDraft(personalization.instructions || "");
      setSources(Array.isArray(guidelines.blocks) ? guidelines.blocks : []);
    } catch (error) {
      setLoadError(formatSettingsLoadError(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const byteCount = useMemo(() => new TextEncoder().encode(draft).length, [draft]);
  const maxBytes = payload?.max_bytes ?? 32 * 1024;
  const dirty = payload != null && draft !== payload.instructions;
  const tooLarge = byteCount > maxBytes;

  const save = async () => {
    if (!dirty || tooLarge || saving) return;
    setSaving(true);
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/settings/personalization`, {
        method: "PUT",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ instructions: draft }),
      }, { timeoutMessage: "保存自定义指令超时，请重试。" });
      if (!response.ok) {
        const message = errorMessageFromResponseText(await response.text().catch(() => ""), response.statusText);
        throw new Error(message);
      }
      const next = await response.json() as PersonalizationPayload;
      setPayload(next);
      setDraft(next.instructions || "");
      pushToast("自定义指令已保存", "success");
      const guidelines = await fetchWithTimeout(
        `${apiBase()}/api/guidelines`,
        { cache: "no-store", headers: authHeaders() },
        { timeoutMessage: "刷新指令来源超时，请重试。" },
      )
        .then((result) => result.ok ? result.json() as Promise<GuidelinePayload> : null)
        .catch(() => null);
      if (guidelines?.blocks) setSources(guidelines.blocks);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      pushToast(`自定义指令保存失败：${message}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const updateMemoryMode = async (enabled: boolean) => {
    if (!conversationId || savingMemoryMode || memoryPolluted) return;
    const nextMode = enabled ? "enabled" : "disabled";
    if (nextMode === memoryMode) return;
    setSavingMemoryMode(true);
    try {
      const result = await sendClientCommandAwaitResult({
        type: "conversation.memory_mode.set",
        conversation_id: conversationId,
        memory_mode: nextMode,
      }, "conversation.memory_mode.set");
      if (!commandResultSucceeded(result)) {
        pushToast(`长期记忆设置失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      pushToast(enabled ? "当前任务已启用长期记忆。" : "当前任务已关闭长期记忆。", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`长期记忆设置失败：${message}`, "error");
    } finally {
      setSavingMemoryMode(false);
    }
  };

  const resetMemory = async () => {
    if (resettingMemory) return;
    const confirmed = await showConfirm({
      title: "清除长期记忆",
      message: "这会清除所有生成的长期记忆和派生摘要。任务对话、目标以及每个任务选择的记忆模式会保留。",
      confirmLabel: "清除记忆",
      danger: true,
    });
    if (!confirmed) return;

    setResettingMemory(true);
    try {
      const result = await sendClientCommandAwaitResult(
        { type: "memory.reset", confirmed: true },
        "memory.reset",
      );
      if (!commandResultSucceeded(result)) {
        pushToast(`记忆清除失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      pushToast("长期记忆已清除，任务对话和记忆模式已保留。", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`记忆清除失败：${message}`, "error");
    } finally {
      setResettingMemory(false);
    }
  };

  const clearPollution = async () => {
    if (!conversationId || clearingPollution) return;
    const confirmed = await showConfirm({
      title: "重新启用任务记忆",
      message: "此任务包含联网、浏览器或 MCP 外部上下文。重新启用后，后续回复可以再次生成长期记忆。",
      confirmLabel: "重新启用",
    });
    if (!confirmed) return;
    setClearingPollution(true);
    try {
      const result = await sendClientCommandAwaitResult({
        type: "conversation.memory_mode.set",
        conversation_id: conversationId,
        memory_mode: "enabled",
      }, "conversation.memory_mode.set");
      if (!commandResultSucceeded(result)) {
        pushToast(`任务记忆恢复失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      pushToast("当前任务已重新启用长期记忆。", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`任务记忆恢复失败：${message}`, "error");
    } finally {
      setClearingPollution(false);
    }
  };

  const instructionsContent = loading ? (
    <div className="settings-personalization-state"><LoaderCircle className="settings-spin" />正在读取指令…</div>
  ) : loadError || !payload ? (
    <div className="settings-load-state" data-tone="danger" role="alert">
      <span className="settings-load-state-icon" aria-hidden="true"><ServerOff /></span>
      <div className="settings-load-state-copy">
        <strong>自定义指令暂时不可用</strong>
        <span>检查后端连接后重试。</span>
        <code>{loadError || "未知错误"}</code>
      </div>
      <button type="button" className="settings-action-button" onClick={() => void load()}>重试</button>
    </div>
  ) : (
    <>
      <Section title="自定义指令" description="所有任务共用的用户级 INSTRUCTIONS.md 指令。">
        <div className="settings-instructions-editor">
          <div className="settings-instructions-toolbar">
            <div className="settings-instructions-file">
              <FileText aria-hidden="true" />
              <span>{fileName(payload.path)}</span>
              <code title={payload.path}>{payload.path}</code>
            </div>
            <button
              type="button"
              className="settings-action-button"
              data-primary="true"
              onClick={() => void save()}
              disabled={!dirty || tooLarge || saving}
            >
              {saving ? <LoaderCircle className="settings-spin" /> : dirty ? <Save /> : <Check />}
              {saving ? "保存中" : dirty ? "保存" : "已保存"}
            </button>
          </div>
          <textarea
            className="settings-instructions-textarea"
            aria-label="自定义指令"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="例如：优先复用现有结构；完成修改后统一运行测试。"
            spellCheck={false}
          />
          <div className="settings-instructions-footer" data-invalid={tooLarge ? "true" : "false"}>
            <span>保存到用户级 INSTRUCTIONS.md。</span>
            <span>{byteCount.toLocaleString()} / {maxBytes.toLocaleString()} 字节</span>
          </div>
        </div>
      </Section>

      <div className="settings-personalization-note">
        <Info aria-hidden="true" />
        <span>项目级指令随后加载，可补充当前代码库规则。</span>
      </div>
    </>
  );

  return (
    <>
      {instructionsContent}

      <Section title="当前任务记忆" description="控制当前任务是否参与长期记忆生成。">
        {memoryPolluted && (
          <div className="settings-memory-isolation" role="status">
            <ShieldAlert aria-hidden="true" />
            <div className="settings-memory-isolation-copy">
              <strong>外部上下文已隔离</strong>
              <span>此任务不会生成或传递新的长期记忆。</span>
              {memoryPollutionSources.length > 0 && (
                <span className="settings-memory-isolation-sources">
                  {Array.from(new Set(memoryPollutionSources.map(pollutionSourceLabel))).map((source) => (
                    <span key={source}>{source}</span>
                  ))}
                </span>
              )}
            </div>
            <button
              type="button"
              className="settings-action-button"
              disabled={clearingPollution}
              onClick={() => void clearPollution()}
            >
              {clearingPollution ? <LoaderCircle className="settings-spin" /> : <RotateCcw />}
              {clearingPollution ? "恢复中" : "重新启用"}
            </button>
          </div>
        )}
        <div className="settings-card">
          <div className="settings-row settings-memory-row">
            <span className="settings-memory-icon" aria-hidden="true"><Brain /></span>
            <div className="settings-row-copy">
              <div className="settings-row-title">长期记忆生成</div>
              <div className="settings-row-description">任务空闲后提取可复用信息，并由单写者统一归并。</div>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-toggle"
                role="switch"
                aria-label="长期记忆生成"
                aria-checked={memoryMode === "enabled"}
                data-active={memoryMode === "enabled" ? "true" : "false"}
                disabled={!conversationId || memoryPolluted || savingMemoryMode}
                onClick={() => void updateMemoryMode(memoryMode !== "enabled")}
              >
                <span />
              </button>
            </div>
          </div>
          <div className="settings-row settings-memory-row">
            <span className="settings-memory-icon" aria-hidden="true"><Trash2 /></span>
            <div className="settings-row-copy">
              <div className="settings-row-title">清除长期记忆</div>
              <div className="settings-row-description">移除生成的记忆和派生摘要，保留任务对话与记忆模式。</div>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-action-button"
                data-danger="true"
                disabled={resettingMemory}
                onClick={() => void resetMemory()}
              >
                {resettingMemory ? <LoaderCircle className="settings-spin" /> : <Trash2 />}
                {resettingMemory ? "清除中" : "清除记忆"}
              </button>
            </div>
          </div>
        </div>
        {!conversationId && <p className="settings-page-note">新建任务后可设置记忆。</p>}
      </Section>

      {!loading && !loadError && payload && (
        <Section title="当前指令来源" description="按作用域加载，项目指令优先。">
          <div className="settings-source-list">
            {sources.length > 0 ? sources.map((source, index) => (
              <div className="settings-source-row" key={`${source.path}-${index}`}>
                <span className="settings-source-icon" aria-hidden="true"><FileText /></span>
                <div className="settings-source-copy">
                  <strong>{fileName(source.path)}</strong>
                  <code title={source.path}>{source.path}</code>
                </div>
                <span className="settings-source-kind">{sourceLabel(source)}</span>
              </div>
            )) : (
              <div className="settings-empty-inline">暂无指令文件。</div>
            )}
          </div>
        </Section>
      )}
    </>
  );

};
