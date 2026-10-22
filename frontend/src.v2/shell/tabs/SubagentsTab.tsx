import { useI18n, type Translate, type TranslationKey } from "../../i18n";
/**
 * Calm, user-facing view of delegated work.
 *
 * Protocol records and runtime diagnostics belong in
 * Inspector. This panel only answers: what is being worked on, where it stands,
 * and what result is available.
 */
import { PromptSourceIcon } from "../../lib/prompt-source-icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { projectMessagesToTurns } from "../../chat/chatSurfaceState";
import { ChatTurn } from "../../chat/components/ChatTurn";
import {
  hydrateMessages,
  type BackendTranscriptMessage,
} from "../../chat/transcriptHydration";
import { AgentAvatar } from "../../components/AgentAvatar";
import { subagentModelPatch } from "../../lib/subagent-model";
import { formatModelLabel } from "../../lib/model-label";
import { workspaceRootsEqual } from "../../lib/workspace-path";
import { MarkdownRenderer } from "../../chat/messages/MarkdownRenderer";
import { fromMarkdown, markdownNodeText } from "../../lib/markdown";
import { SubagentPlanReviewCard } from "../../chat/InlineAgentPrompt";
import {
  projectAgentViews,
  type AgentView,
  type AgentLink,
} from "../../lib/agent-view-model";
import {
  commandResultSucceeded,
  sendClientCommandAwaitResult,
} from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
import type { ChatMessage, PendingAskUser, SubagentState } from "../../stores/types";
import { pushToast } from "../../overlays/ToastContainer";
import "./SubagentsTab.css";

type TranscriptPresentationSource = "none" | "durable" | "push";

type TranscriptPresentation = {
  ownerId: string | null;
  conversationId: string | null;
  workspaceRoot: string;
  messages: ChatMessage[];
  seq: number;
  source: TranscriptPresentationSource;
};

const emptyTranscriptPresentation = (): TranscriptPresentation => ({
  ownerId: null,
  conversationId: null,
  workspaceRoot: "",
  messages: [],
  seq: -1,
  source: "none",
});

const AGENT_UI_TEXT: Record<string, string> = {
  "Needs attention": "需要处理",
  "Running": "运行中",
  "Waiting": "等待中",
  "Completed": "已完成",
  "Collecting results": "整理结果中",
  "Skipped": "已跳过",
  "Queued": "排队中",
  "Result retained": "已保留结果",
  "Partially completed": "部分完成",
  "Stopped": "已停止",
  "Cancelled": "已取消",
  "Failed": "失败",
  "Merged into main task": "已由主任务接管",
  "Task failed": "任务执行失败",
  "Available result retained": "已保留可用结果",
  "Partial work completed": "已完成部分工作",
  "Stopped by you": "已由你停止",
  "Task cancelled": "任务已取消",
  "Waiting for prerequisite work": "等待前置任务完成",
  "Waiting to start": "等待启动",
  "Task completed": "任务已完成",
  "Working": "正在执行",
  "Required read or search tools are unavailable": "缺少必要的读取或搜索能力",
  "A matching task is already running": "相同任务已在处理中",
  "Queued behind other delegated work": "任务较多，正在依次处理",
  "Paused; available results were kept": "任务已暂停，现有结果已保留",
};

const agentUiText = (value: string): string => AGENT_UI_TEXT[value] ?? value;

const relativeTimeText = (value: string, t: Translate): string => {
  if (!value) return "";
  if (value === "Just now" || value === "刚刚") return t("刚刚");
  const match = value.match(/^(\d+)\s*(分钟|小时|天)$/);
  if (match) return t(match[2] === "分钟" ? "{count}分钟前" : match[2] === "小时" ? "{count}小时前" : "{count}天前", { count: match[1] });
  const englishMatch = value.match(/^(\d+)\s*(m|h|d)\s+ago$/i);
  if (!englishMatch) return value;
  const unit = englishMatch[2].toLowerCase() === "m" ? "分钟" : englishMatch[2].toLowerCase() === "h" ? "小时" : "天";
  return t(unit === "分钟" ? "{count}分钟前" : unit === "小时" ? "{count}小时前" : "{count}天前", { count: englishMatch[1] });
};

const statusText = (view: AgentView, t: Translate): string => t(agentUiText(view.statusLabel) as TranslationKey);
const agentElapsedText = (startedAt: number | undefined, now: number, t: Translate): string => {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  if (seconds < 1) return "";
  if (seconds < 60) return t("{seconds}秒", { seconds });
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return t("{minutes}分钟", { minutes }) + (remainder ? t("{seconds}秒", { seconds: remainder }) : "");
  const hours = Math.floor(minutes / 60);
  return t("{hours}小时", { hours }) + (minutes % 60 ? t("{minutes}分钟", { minutes: minutes % 60 }) : "") + (remainder ? t("{seconds}秒", { seconds: remainder }) : "");
};
const AgentGlyph = ({ view, large = false }: { view: AgentView; large?: boolean }) => {
  return (
    <AgentAvatar
      className="subagents-glyph"
      identityKey={view.identityKey}
      status={view.status}
      showStatus={false}
      size={large ? "large" : "medium"}
    />
  );
};

const AgentRow = ({
  view,
  source,
  now,
  onOpen,
}: {
  view: AgentView;
  source: SubagentState;
  now: number;
  onOpen: () => void;
}) => {
  const { t } = useI18n();
  const active = view.canStop;
  const objective = source.objective
    ? markdownNodeText(fromMarkdown(source.objective)).replace(/\s+/g, " ").trim().slice(0, 60)
    : "";
  const preview = objective && objective !== view.title ? objective : !objective && active && view.status === "running" ? t("正在执行") : "";
  const waiting = active && view.status !== "running";
  const elapsed = active ? agentElapsedText(view.startedAt, now, t) : "";
  return (
  <button
    type="button"
    className="subagents-row"
    data-status={view.status}
    data-preview={preview ? "true" : "false"}
    aria-label={t("打开子智能体任务：{name}", { name: view.title })}
    title={`${view.title} · ${statusText(view, t)}`}
    onClick={onOpen}
  >
    <AgentGlyph view={view} />
    <span className="subagents-row-copy">
      <span className="subagents-row-heading">
        <span className="subagents-row-title">{view.title}</span>
        {active ? <span className="subagents-row-trailing">
          {waiting && <span className="subagents-row-status">{statusText(view, t)}</span>}
          {elapsed && <span className="subagents-row-time">{elapsed}</span>}
        </span> : view.relativeTimeLabel && (
          <span className="subagents-row-time">{relativeTimeText(view.relativeTimeLabel, t)}</span>
        )}
      </span>
      {preview && <span className="subagents-row-preview">{preview}</span>}
      {!waiting && <span className="sr-only">{statusText(view, t)}</span>}
    </span>
  </button>
  );
};

const AgentRelationLink = ({ agent }: { agent: AgentLink }) => (
  <button type="button" className="subagents-relation-link" onClick={() => useAppStore.getState().setFocusedSubagentId(agent.id)}>
    <AgentAvatar identityKey={agent.identityKey} size="small" showStatus={false} />
    <span>{agent.title}</span>
  </button>
);

const AgentDetail = ({
  view,
  onBack,
  transcriptMessages,
  workspaceRoot,
  conversationId,
  transcriptLoading,
  transcriptError,
  source,
  planRequest,
}: {
  view: AgentView;
  onBack: () => void;
  transcriptMessages: ChatMessage[];
  workspaceRoot: string;
  /** The parent conversation owns the child journal's persisted artifacts. */
  conversationId?: string;
  transcriptLoading: boolean;
  transcriptError: string;
  source: SubagentState;
  planRequest?: PendingAskUser;
}) => {
  const { t } = useI18n();
  const isLive = view.status === "running" || view.status === "waiting";
  const executionWorkspaceRoot = source.workspaceRoot ?? "";
  const turns = useMemo(
    () => projectMessagesToTurns(
      transcriptMessages,
      isLive,
      executionWorkspaceRoot,
    ),
    [isLive, transcriptMessages, executionWorkspaceRoot],
  );
  const finalAnswer = turns.at(-1)?.finalAnswerCell;
  const hasCanonicalResult = Boolean(finalAnswer && !finalAnswer.isStreaming
    && (finalAnswer.markdownSource.trim() || finalAnswer.artifacts?.length));

  return (
    <section className="subagents-detail" aria-label={t("子智能体任务详情：{name}", { name: view.title })}>
      <header className="subagents-detail-header">
        <button
          type="button"
          className="subagents-back"
          aria-label={t("返回子智能体列表")}
          onClick={onBack}
        >
          <PromptSourceIcon icon="arrow-left" />
        </button>
        <AgentGlyph view={view} large />
        <span className="subagents-detail-heading">
          <strong title={view.title}>{view.title}</strong>
        </span>
        {view.model && <span className="subagents-detail-model" title={[view.provider, view.model, view.reasoningEffort].filter(Boolean).join(" · ")}>
          {formatModelLabel(view.model)}{view.reasoningEffort ? ` · ${view.reasoningEffort}` : ""}
        </span>}
      </header>

      <div className="subagents-detail-body">
        <div className="subagents-context" aria-label={t("任务关系")}>
          {view.status !== "running" && (view.status !== "completed" || turns.length === 0) && <span role="status">{statusText(view, t)}</span>}
          {view.effectiveStatus === "blocked" && view.blockedDependencies.length > 0 && <div className="subagents-relation">
            <span>{t("正在等待")}</span>
            {view.blockedDependencies.map((agent) => <AgentRelationLink key={agent.id} agent={agent} />)}
          </div>}
        </div>
        {planRequest?.planReview && <SubagentPlanReviewCard request={planRequest} review={planRequest.planReview} />}
        {view.status === "attention" && !planRequest && !view.resultError && !source.cleanupPending && (
          <div className="subagents-attention" role="status">{view.summary}</div>
        )}
        {view.canStop && view.effectiveStatus !== "running" && view.statusLabel === t("清理未完成") && (
          <div className="subagents-transcript-error" role="status">{view.summary}</div>
        )}
        {view.resultError && (
          <div className="subagents-transcript-error" role="alert">
            <span>{view.resultError}</span>
          </div>
        )}
        {transcriptError && (
          <div className="subagents-transcript-error" role="status">
            <span>{transcriptError}</span>
          </div>
        )}
        {transcriptLoading && transcriptMessages.length === 0 && (
          <div className="subagents-transcript-loading">{t("正在载入工作详情…")}</div>
        )}
        {!transcriptLoading && !transcriptError && turns.length === 0 && (
          <div className="subagents-transcript-empty">
            {isLive ? t("子智能体正在启动，工作记录会实时显示在这里。") : view.resultContent ? t("此任务保留了结果，暂无可回放的工作记录。") : t("这个子智能体没有可回放的工作记录。")}
          </div>
        )}
        {view.resultContent && !hasCanonicalResult && <div className="subagents-retained-result" aria-label={t("保留结果")}>
          <div className="subagents-retained-result-content md-prose"><MarkdownRenderer content={view.resultContent} conversationId={conversationId} workspaceRoot={executionWorkspaceRoot} /></div>
        </div>}
        {turns.length > 0 && (
          <div className="subagents-transcript" aria-label={t("子智能体工作记录")}>
            {turns.map((turn) => (
              <ChatTurn
                key={turn.id}
                turn={turn}
                isTranscriptMode
                conversationId={conversationId}
                workspaceRoot={executionWorkspaceRoot}
                allowFileRevert={Boolean(source.workspaceRoot && workspaceRootsEqual(source.workspaceRoot, workspaceRoot))}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
};

export const SubagentsTab = () => {
  const { t } = useI18n();
  const subagents = useAppStore((state) => state.subagents);
  const [now, setNow] = useState(Date.now);
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(() => new Set());
  const hasStartedActive = subagents.some((agent) => ["running", "blocked"].includes(agent.status) && Boolean(agent.startedAt));
  useEffect(() => {
    if (!hasStartedActive) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [hasStartedActive]);
  const selectedAgentId = useAppStore((state) => state.focusedSubagentId);
  const setSelectedAgentId = useAppStore((state) => state.setFocusedSubagentId);
  const pendingActionRef = useRef<{ id: string; conversationId: string; workspaceRoot: string } | null>(null);
  const [transcriptPresentation, setTranscriptPresentation] = useState<TranscriptPresentation>(
    emptyTranscriptPresentation,
  );
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState("");
  const transcriptRequestRef = useRef(0);
  const actionRequestRef = useRef(0);
  const transcriptPresentationRef = useRef<TranscriptPresentation>(emptyTranscriptPresentation());
  const views = projectAgentViews(subagents);
  const selectedView = views.find((view) => view.id === selectedAgentId);
  const selectedAgent = subagents.find((agent) => agent.id === selectedAgentId);
  const pendingAskUser = useAppStore((state) => state.pendingAskUser);
  const askUserQueue = useAppStore((state) => state.askUserQueue);
  const conversationId = useAppStore((state) => state.conversationId);
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const visibleTranscript = transcriptPresentation.ownerId === selectedAgentId
    && transcriptPresentation.conversationId === conversationId
    && transcriptPresentation.workspaceRoot === workingDirectory
    ? transcriptPresentation
    : emptyTranscriptPresentation();
  const commitTranscriptPresentation = useCallback((next: TranscriptPresentation) => {
    transcriptPresentationRef.current = next;
    setTranscriptPresentation(next);
  }, []);

  const loadTranscript = useCallback(async (id: string) => {
    if (!conversationId) return;
    const requestId = ++transcriptRequestRef.current;
    const ownerConversationId = conversationId;
    setTranscriptLoading(true);
    setTranscriptError("");
    try {
      const result = await sendClientCommandAwaitResult({
        type: "subagent.transcript",
        subagent_id: id,
        conversation_id: conversationId,
        workspace_root: workingDirectory || undefined,
      }, "subagent.transcript", { silent: true });
      if (
        requestId !== transcriptRequestRef.current
        || useAppStore.getState().conversationId !== ownerConversationId
        || useAppStore.getState().workingDirectory !== workingDirectory
        || useAppStore.getState().focusedSubagentId !== id
      ) return;
      const currentAgent = useAppStore.getState().subagents.find((agent) => agent.id === id);
      const responseEpoch = result.data?.mailbox_epoch;
      const responsePath = result.data?.agent_path;
      if (typeof responseEpoch === "number" && typeof currentAgent?.mailboxEpoch === "number"
        && (responseEpoch < currentAgent.mailboxEpoch || (responseEpoch === currentAgent.mailboxEpoch
          && typeof responsePath === "string" && currentAgent.agentPath && responsePath !== currentAgent.agentPath))) return;
      const resultLevel = String(result.level || "").toLowerCase();
      if (!commandResultSucceeded(result)) {
        setTranscriptError(result.message || "无法读取子智能体工作记录。");
        return;
      }
      const rawMessages = Array.isArray(result.data?.messages)
        ? result.data.messages as BackendTranscriptMessage[]
        : [];
      const responseSeq = Number(result.data?.seq ?? 0);
      const currentPresentation = transcriptPresentationRef.current;
      if (
        currentPresentation.ownerId === id
        && currentPresentation.conversationId === ownerConversationId
        && currentPresentation.workspaceRoot === workingDirectory
        && Number.isFinite(responseSeq)
        && (
          responseSeq < currentPresentation.seq
          || (responseSeq === currentPresentation.seq && currentPresentation.source === "push")
        )
      ) return;
      const responseWorkspaceRoot = result.data?.workspace_root;
      useAppStore.getState().updateSubagent(id, {
        ...subagentModelPatch(result.data ?? {}, currentAgent),
        ...(typeof responseWorkspaceRoot === "string" ? { workspaceRoot: responseWorkspaceRoot } : {}),
        ...(typeof result.data?.accepted_at === "number" ? { acceptedAt: result.data.accepted_at } : {}),
        ...(typeof result.data?.started_at === "number" ? { startedAt: result.data.started_at } : {}),
        ...(typeof result.data?.completed_at === "number" ? { completedAt: result.data.completed_at }
          : result.data?.completed_at === null ? { completedAt: undefined } : {}),
      }, ownerConversationId);
      if (resultLevel === "warning") {
        setTranscriptError(result.message || "无法读取子智能体工作记录。");
        return;
      }
      const nextSeq = Number.isFinite(responseSeq) ? responseSeq : 0;
      const hydrated = hydrateMessages(rawMessages);
      commitTranscriptPresentation({
        ownerId: id,
        conversationId: ownerConversationId,
        workspaceRoot: workingDirectory,
        messages: hydrated,
        seq: nextSeq,
        source: "durable",
      });
    } catch (error) {
      if (requestId !== transcriptRequestRef.current
        || useAppStore.getState().conversationId !== ownerConversationId
        || useAppStore.getState().workingDirectory !== workingDirectory
        || useAppStore.getState().focusedSubagentId !== id) return;
      setTranscriptError(error instanceof Error ? error.message : "无法读取子智能体工作记录。");
    } finally {
      if (requestId === transcriptRequestRef.current) setTranscriptLoading(false);
    }
  }, [conversationId, workingDirectory, commitTranscriptPresentation]);

  useEffect(() => {
    pendingActionRef.current = null;
    actionRequestRef.current += 1;
    transcriptRequestRef.current += 1;
    setTranscriptLoading(false);
    setTranscriptError("");
    if (!selectedAgentId || !conversationId) {
      commitTranscriptPresentation(emptyTranscriptPresentation());
      return;
    }
    const current = useAppStore.getState().subagents.find((agent) => agent.id === selectedAgentId);
    const hasPushedSnapshot = current?.transcriptSeq != null;
    commitTranscriptPresentation({
      ownerId: selectedAgentId,
      conversationId,
      workspaceRoot: workingDirectory,
      messages: current?.transcriptMessages ?? [],
      seq: current?.transcriptSeq ?? -1,
      source: hasPushedSnapshot ? "push" : "none",
    });
    void loadTranscript(selectedAgentId);
  }, [
    conversationId,
    selectedAgentId,
    workingDirectory,
    commitTranscriptPresentation,
    loadTranscript,
  ]);

  useEffect(() => {
    if (!selectedAgentId || selectedAgent?.transcriptSeq == null) return;
    const pushedMessages = selectedAgent.transcriptMessages ?? [];
    const currentPresentation = transcriptPresentationRef.current;
    if (
      currentPresentation.ownerId === selectedAgentId
      && currentPresentation.conversationId === conversationId
      && currentPresentation.workspaceRoot === workingDirectory
      && selectedAgent.transcriptSeq <= currentPresentation.seq
    ) return;
    transcriptRequestRef.current += 1;
    setTranscriptLoading(false);
    setTranscriptError("");
    commitTranscriptPresentation({
      ownerId: selectedAgentId,
      conversationId,
      workspaceRoot: workingDirectory,
      messages: pushedMessages,
      seq: selectedAgent.transcriptSeq,
      source: "push",
    });
  }, [
    selectedAgent?.transcriptMessages,
    selectedAgent?.transcriptSeq,
    selectedAgentId,
    conversationId,
    workingDirectory,
    commitTranscriptPresentation,
  ]);

  const fetchResult = useCallback(async (id: string) => {
    if (!conversationId) return;
    if (pendingActionRef.current?.id === id) return;
    const ownerConversationId = conversationId;
    const requestId = ++actionRequestRef.current;
    const action = { id, conversationId, workspaceRoot: workingDirectory };
    pendingActionRef.current = action;
    try {
      const result = await sendClientCommandAwaitResult({
        type: "subagent.status",
        subagent_id: id,
        include_result: true,
        conversation_id: conversationId,
        workspace_root: workingDirectory || undefined,
      }, "subagent.status");
      if (requestId !== actionRequestRef.current || useAppStore.getState().conversationId !== ownerConversationId
        || useAppStore.getState().workingDirectory !== workingDirectory) return;
      if (!commandResultSucceeded(result)) {
        pushToast(result.message || "获取子智能体结果失败。", "error", 4000);
      } else if (String(result.level || "").toLowerCase() === "warning" && result.message) {
        pushToast(result.message, "warning", 3500);
      }
    } catch (error) {
      if (requestId !== actionRequestRef.current || useAppStore.getState().conversationId !== ownerConversationId
        || useAppStore.getState().workingDirectory !== workingDirectory) return;
      pushToast(error instanceof Error ? error.message : "获取子智能体结果失败。", "error", 4000);
    } finally {
      if (pendingActionRef.current === action) pendingActionRef.current = null;
    }
  }, [conversationId, workingDirectory]);

  const transcriptHasResult = visibleTranscript.messages.some((message) => message.role === "assistant"
    && !message.isStreaming && Boolean(message.content.trim()));
  useEffect(() => {
    if (selectedView?.needsResult && !transcriptLoading && !transcriptHasResult
      && (visibleTranscript.source !== "none" || transcriptError)) void fetchResult(selectedView.id);
  }, [selectedView?.id, selectedView?.needsResult, transcriptLoading, transcriptHasResult, visibleTranscript.source, transcriptError, fetchResult]);

  if (selectedView && selectedAgent) {
    const planRequest = [pendingAskUser, ...askUserQueue].find((request) =>
      ["pending", "running", "blocked"].includes(selectedAgent.status)
      && request?.conversationId === conversationId && request?.planReview?.subagentId === selectedView.id
      && (!selectedAgent.activePlanRequestId || selectedAgent.activePlanRequestId === request.requestId),
    ) ?? undefined;
    return (
      <AgentDetail
        key={`${conversationId}:${selectedView.id}`}
        view={selectedView}
        source={selectedAgent}
        planRequest={planRequest}
        onBack={() => setSelectedAgentId(null)}
        transcriptMessages={visibleTranscript.messages}
        workspaceRoot={workingDirectory}
        conversationId={conversationId || undefined}
        transcriptLoading={transcriptLoading}
        transcriptError={transcriptError}
      />
    );
  }

  const overviewViews = views.filter((view) => {
    if (["error", "cancelled"].includes(view.effectiveStatus)) return false;
    const source = subagents.find((agent) => agent.id === view.id)!;
    const latestStatus = source.transcriptMessages?.filter((message) => message.role === "assistant").at(-1)?.terminalStatus;
    return view.canStop || (latestStatus !== "failed" && latestStatus !== "interrupted");
  });
  const activeViews = overviewViews.filter((view) => view.canStop);
  const completedViews = overviewViews.filter((view) => !view.canStop);
  const waitingCount = activeViews.filter((view) => view.status !== "running").length;
  const groups = [
    { key: "active", label: t("进行中"), items: activeViews, initialLimit: 4 },
    { key: "completed", label: t("已完成"), items: completedViews, initialLimit: 10 },
  ] as const;

  return (
    <div className="subagents-tab">
      <div className="subagents-groups">
        {groups.map((group) => {
          const items = group.items;
          if (items.length === 0 && group.key !== "active") return null;
          const disclosureKey = `${conversationId}:${group.key}`;
          const expanded = expandedGroups.has(disclosureKey);
          const visibleItems = expanded ? items : items.slice(0, group.initialLimit);

          return (
            <section
              key={group.key}
              className="subagents-group"
              aria-label={t("{label}，{count} 项", { label: group.label, count: items.length })}
            >
              <div className="subagents-group-heading">
                <span>{group.key === "active" ? t("已开启") : t("完成")} · {items.length}</span>
                {group.key === "active" && waitingCount > 0 && <span>{t("{count} 个等待中", { count: waitingCount })}</span>}
              </div>
              <div className="subagents-list">
                {group.key === "active" && items.length === 0 && <div className="subagents-group-empty">{t("没有已开启的子代理")}</div>}
                {visibleItems.map((view) => (
                  <AgentRow
                    key={view.id}
                    view={view}
                    source={subagents.find((agent) => agent.id === view.id)!}
                    now={now}
                    onOpen={() => {
                      setSelectedAgentId(view.id);
                    }}
                  />
                ))}
              </div>
              {items.length > group.initialLimit && <button type="button" className="subagents-show-more"
                aria-expanded={expanded} aria-label={expanded ? t("收起{label}子智能体", { label: group.label }) : t("再显示 {count} 个{label}子智能体", { count: items.length - group.initialLimit, label: group.label })}
                onClick={() => setExpandedGroups((current) => {
                  const next = new Set(current);
                  if (next.has(disclosureKey)) next.delete(disclosureKey);
                  else next.add(disclosureKey);
                  return next;
                })}>{expanded ? t("收起") : t("再显示 {count} 个", { count: items.length - group.initialLimit })}</button>}
            </section>
          );
        })}
      </div>
    </div>
  );
};
