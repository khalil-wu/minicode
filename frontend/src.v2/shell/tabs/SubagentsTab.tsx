/**
 * Calm, user-facing view of delegated work.
 *
 * Protocol records and runtime diagnostics belong in
 * Inspector. This panel only answers: what is being worked on, where it stands,
 * and what result is available.
 */
import { ArrowLeft, Bot, ChevronDown, ChevronRight } from "lucide-react";
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
import { MarkdownRenderer } from "../../chat/messages/MarkdownRenderer";
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
import { EmptyLine, SmallButton } from "../SidebarShared";
import "./SubagentsTab.css";

const COMPLETED_PREVIEW_LIMIT = 10;

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

const relativeTimeText = (value: string): string => {
  if (!value) return "";
  if (value === "Just now") return "刚刚";
  const match = value.match(/^(\d+)\s*(分钟|小时|天)$/);
  if (match) return `${match[1]}${match[2]}前`;
  const englishMatch = value.match(/^(\d+)\s*(m|h|d)\s+ago$/i);
  if (!englishMatch) return value;
  const unit = englishMatch[2].toLowerCase() === "m" ? "分钟" : englishMatch[2].toLowerCase() === "h" ? "小时" : "天";
  return `${englishMatch[1]}${unit}前`;
};

const statusText = (view: AgentView): string => agentUiText(view.statusLabel);
const summaryText = (view: AgentView): string => agentUiText(view.summary);

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
  durationMs,
  onOpen,
}: {
  view: AgentView;
  durationMs?: number;
  onOpen: () => void;
}) => (
  <button
    type="button"
    className="subagents-row"
    data-status={view.status}
    aria-label={`打开子智能体任务：${view.title}`}
    title={view.title}
    onClick={onOpen}
  >
    <AgentGlyph view={view} />
    <span className="subagents-row-copy">
      <span className="subagents-row-heading">
        <span className="subagents-row-title">{view.teammateName || view.title}</span>
        {view.status !== "completed" && durationMs != null && durationMs >= 1000 && (
          <span className="subagents-row-time">{Math.floor(durationMs / 60000)}分{Math.floor(durationMs / 1000) % 60}秒</span>
        )}
        {view.status === "completed" && view.relativeTimeLabel && (
          <span className="subagents-row-time">{relativeTimeText(view.relativeTimeLabel)}</span>
        )}
      </span>
      <span className={view.status === "completed" ? "sr-only" : "subagents-row-meta"}>
        <span className={view.status === "running" ? "sr-only" : "subagents-row-status"}>{statusText(view)}</span>
        {view.status !== "completed" && view.summary && view.summary !== view.title && (
          <span className="subagents-row-summary">{summaryText(view)}</span>
        )}
      </span>
    </span>
    <ChevronRight className="subagents-row-chevron" size={14} aria-hidden="true" />
  </button>
);

const AgentRelationLink = ({ agent }: { agent: AgentLink }) => (
  <button type="button" className="subagents-relation-link" onClick={() => useAppStore.getState().setFocusedSubagentId(agent.id)}>
    <AgentAvatar identityKey={agent.identityKey} size="small" showStatus={false} />
    <span>{agent.title}</span>
  </button>
);

const AgentDetail = ({
  view,
  onBack,
  onFetchResult,
  transcriptMessages,
  workspaceRoot,
  conversationId,
  transcriptLoading,
  transcriptError,
  onRefreshTranscript,
  pendingAction,
  source,
  agents,
  planRequest,
}: {
  view: AgentView;
  onBack: () => void;
  onFetchResult: () => void;
  transcriptMessages: ChatMessage[];
  workspaceRoot: string;
  /** The parent conversation owns the child journal's persisted artifacts. */
  conversationId?: string;
  transcriptLoading: boolean;
  transcriptError: string;
  onRefreshTranscript: () => void;
  pendingAction: "result" | null;
  source: SubagentState;
  agents: AgentView[];
  planRequest?: PendingAskUser;
}) => {
  const isLive = view.status === "running" || view.status === "waiting";
  const turns = useMemo(
    () => projectMessagesToTurns(
      transcriptMessages,
      isLive,
      workspaceRoot,
    ),
    [isLive, transcriptMessages, workspaceRoot],
  );
  const finalAnswer = turns.at(-1)?.finalAnswerCell;
  const hasCanonicalResult = Boolean(finalAnswer && !finalAnswer.isStreaming
    && (finalAnswer.markdownSource.trim() || finalAnswer.artifacts?.length));

  return (
    <section className="subagents-detail" aria-label={`子智能体任务详情：${view.title}`}>
      <header className="subagents-detail-header">
        <button
          type="button"
          className="subagents-back"
          aria-label="返回子智能体列表"
          onClick={onBack}
        >
          <ArrowLeft size={16} />
        </button>
        <AgentGlyph view={view} large />
        <span className="subagents-detail-heading">
          <strong title={view.title}>{view.teammateName || view.title}</strong>
        </span>
        {view.model && <span className="subagents-detail-model" title={[view.provider, view.model, view.reasoningEffort].filter(Boolean).join(" · ")}>
          {formatModelLabel(view.model)}{view.reasoningEffort ? ` · ${view.reasoningEffort}` : ""}
        </span>}
      </header>

      <div className="subagents-detail-body">
        <div className="subagents-context" aria-label="任务关系">
          {(view.status !== "completed" || turns.length === 0) && <span role="status">{statusText(view)}</span>}
          {(view.teamName || view.parent) && <div className="subagents-identity-row">
            {view.teamName && <span className="subagents-identity-name">{view.teamName}</span>}
            {view.teamName && view.parent && <span aria-hidden="true">·</span>}
            {view.parent && <div className="subagents-relation"><span>由</span><AgentRelationLink agent={view.parent} /><span>委派</span></div>}
          </div>}
          {(view.blockedDependencies.length > 0 || view.dependencies.length > 0) && <div className="subagents-relation">
            <span>{view.blockedDependencies.length > 0 ? "正在等待" : "前置任务"}</span>
            {(view.blockedDependencies.length > 0 ? view.blockedDependencies : view.dependencies).map((agent) => <AgentRelationLink key={agent.id} agent={agent} />)}
          </div>}
          {view.children.length > 0 && <div className="subagents-relation"><span>已委派</span>{view.children.map((agent) => <AgentRelationLink key={agent.id} agent={agent} />)}</div>}
        </div>
        {planRequest?.planReview && <SubagentPlanReviewCard request={planRequest} review={planRequest.planReview} />}
        {view.status === "attention" && !planRequest && !view.resultError && !source.cleanupPending && (
          <div className="subagents-attention" role="status">{view.summary}</div>
        )}
        {view.canStop && view.effectiveStatus !== "running" && view.statusLabel === "清理未完成" && (
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
            <button type="button" onClick={onRefreshTranscript}>重试</button>
          </div>
        )}
        {transcriptLoading && transcriptMessages.length === 0 && (
          <div className="subagents-transcript-loading">正在载入工作详情…</div>
        )}
        {!transcriptLoading && !transcriptError && turns.length === 0 && (
          <div className="subagents-transcript-empty">
            {isLive ? "子智能体正在启动，工作记录会实时显示在这里。" : view.resultContent ? "此任务保留了结果，暂无可回放的工作记录。" : "这个子智能体没有可回放的工作记录。"}
          </div>
        )}
        {view.resultContent && !hasCanonicalResult && <details className="subagents-retained-result" open={turns.length === 0 || undefined}>
          <summary>保留结果</summary>
          <div className="subagents-retained-result-content md-prose"><MarkdownRenderer content={view.resultContent} conversationId={conversationId} workspaceRoot={workspaceRoot} /></div>
        </details>}
        {turns.length > 0 && (
          <div className="subagents-transcript" aria-label="子智能体工作记录">
            {turns.map((turn) => (
              <ChatTurn
                key={turn.id}
                turn={turn}
                isTranscriptMode
                conversationId={conversationId}
                workspaceRoot={workspaceRoot}
              />
            ))}
          </div>
        )}
        {view.needsResult && transcriptMessages.length === 0 && (
          <div className="subagents-detail-actions">
            <SmallButton icon={<ChevronRight size={14} />} label={pendingAction === "result" ? "正在获取" : "获取结果"} onClick={onFetchResult} disabled={pendingAction != null} />
          </div>
        )}
        {source.messages?.length ? <details className="subagents-messages">
          <summary>协作消息 · {source.messages.length}</summary>
          {source.messages.map((message) => {
            const sender = agents.find((agent) => agent.id === message.senderId || agent.teammateName === message.senderId);
            const recipient = agents.find((agent) => agent.id === message.recipientId || agent.teammateName === message.recipientId);
            return <div className="subagents-message" key={message.messageId}>
              <div className="subagents-message-heading">
                {sender ? <AgentRelationLink agent={sender} /> : <span>{message.senderId === "user" ? "你" : message.senderId === "parent" ? "主任务" : "协作者"}</span>}
                <span>→</span>
                {recipient ? <AgentRelationLink agent={recipient} /> : <span>{message.recipientId === "parent" ? "主任务" : "协作者"}</span>}
                {message.deliveryStatus && <small>{message.deliveryStatus === "sent" ? "已投递" : message.deliveryStatus === "sending" ? "发送中" : "发送失败"}</small>}
              </div>
              <p>{message.content}</p>
            </div>;
          })}
        </details> : null}
      </div>
    </section>
  );
};

export const SubagentsTab = () => {
  const subagents = useAppStore((state) => state.subagents);
  const selectedAgentId = useAppStore((state) => state.focusedSubagentId);
  const setSelectedAgentId = useAppStore((state) => state.setFocusedSubagentId);
  const [showAllCompleted, setShowAllCompleted] = useState(false);
  const [pendingAction, setPendingAction] = useState<{ id: string; kind: "result"; conversationId: string; workspaceRoot: string } | null>(null);
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
      useAppStore.getState().updateSubagent(id, subagentModelPatch(result.data ?? {}, currentAgent), ownerConversationId);
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
    setPendingAction(null);
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

  const fetchResult = async (id: string) => {
    if (!conversationId) return;
    if (pendingAction?.id === id) return;
    const ownerConversationId = conversationId;
    const requestId = ++actionRequestRef.current;
    const action = { id, kind: "result" as const, conversationId, workspaceRoot: workingDirectory };
    setPendingAction(action);
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
      setPendingAction((current) => current === action ? null : current);
    }
  };

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
        agents={views}
        planRequest={planRequest}
        onBack={() => setSelectedAgentId(null)}
        onFetchResult={() => void fetchResult(selectedView.id)}
        transcriptMessages={visibleTranscript.messages}
        workspaceRoot={workingDirectory}
        conversationId={conversationId || undefined}
        transcriptLoading={transcriptLoading}
        transcriptError={transcriptError}
        onRefreshTranscript={() => void loadTranscript(selectedView.id)}
        pendingAction={pendingAction?.id === selectedView.id && pendingAction.conversationId === conversationId
          && pendingAction.workspaceRoot === workingDirectory ? pendingAction.kind : null}
      />
    );
  }

  if (views.length === 0) {
    return (
      <div className="subagents-tab">
        <div className="subagents-empty">
          <span className="subagents-empty-icon" aria-hidden="true"><Bot size={20} strokeWidth={1.8} /></span>
          <EmptyLine>暂无子智能体</EmptyLine>
          <span>MiniCode 拆分任务后，委派工作会显示在这里。</span>
        </div>
      </div>
    );
  }

  const activeViews = views.filter((view) => view.status === "running" || view.status === "waiting");
  const attentionViews = views.filter((view) => view.status === "attention");
  const completedViews = views.filter((view) => view.status === "completed");
  const groups = [
    { key: "attention", label: "需要处理", items: attentionViews },
    { key: "active", label: "进行中", items: activeViews },
    { key: "completed", label: "已完成", items: completedViews },
  ] as const;

  return (
    <div className="subagents-tab">
      <div className="subagents-groups">
        {groups.map((group) => {
          const items = group.items;
          if (items.length === 0) return null;
          const canCollapse = group.key === "completed" && items.length > COMPLETED_PREVIEW_LIMIT;
          const visibleItems = canCollapse && !showAllCompleted
            ? items.slice(0, COMPLETED_PREVIEW_LIMIT)
            : items;

          return (
            <section
              key={group.key}
              className="subagents-group"
              aria-label={`${group.label}，${items.length} 项`}
            >
              <div className="subagents-group-heading">
                <span>{group.key === "active" ? "已开启" : group.key === "completed" ? "完成" : group.label} · {items.length}</span>
              </div>
              <div className="subagents-list">
                {visibleItems.map((view) => (
                  <AgentRow
                    key={view.id}
                    view={view}
                    durationMs={subagents.find((agent) => agent.id === view.id)?.durationMs}
                    onOpen={() => {
                      setSelectedAgentId(view.id);
                      if (view.needsResult) void fetchResult(view.id);
                    }}
                  />
                ))}
              </div>
              {canCollapse && (
                <button
                  type="button"
                  className="subagents-show-more"
                  aria-expanded={showAllCompleted}
                  onClick={() => setShowAllCompleted((value) => !value)}
                >
                  {showAllCompleted ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  <span>
                    {showAllCompleted
                      ? "收起已完成任务"
                      : `再显示 ${items.length - COMPLETED_PREVIEW_LIMIT} 项`}
                  </span>
                </button>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
};
