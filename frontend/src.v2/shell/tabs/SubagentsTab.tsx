/**
 * Calm, user-facing view of delegated work.
 *
 * Protocol records and runtime diagnostics belong in
 * Inspector. This panel only answers: what is being worked on, where it stands,
 * and what result is available.
 */
import { ArrowLeft, Bot, ChevronDown, ChevronRight, RefreshCw, Square } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { projectMessagesToTurns } from "../../chat/chatSurfaceState";
import { ChatTurn } from "../../chat/components/ChatTurn";
import {
  hydrateMessages,
  type BackendTranscriptMessage,
} from "../../chat/transcriptHydration";
import { AgentAvatar } from "../../components/AgentAvatar";
import {
  projectAgentViews,
  type AgentView,
} from "../../lib/agent-view-model";
import {
  commandResultSucceeded,
  sendClientCommandAwaitResult,
} from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
import type { ChatMessage } from "../../stores/types";
import { pushToast } from "../../overlays/ToastContainer";
import { EmptyLine, SmallButton } from "../SidebarShared";
import "./SubagentsTab.css";

const COMPLETED_PREVIEW_LIMIT = 6;

type TranscriptPresentationSource = "none" | "durable" | "push";

type TranscriptPresentation = {
  ownerId: string | null;
  messages: ChatMessage[];
  seq: number;
  source: TranscriptPresentationSource;
};

const emptyTranscriptPresentation = (): TranscriptPresentation => ({
  ownerId: null,
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
      tone={view.glyphTone}
      status={view.status}
      size={large ? "large" : "medium"}
    />
  );
};

const AgentRow = ({
  view,
  onOpen,
}: {
  view: AgentView;
  onOpen: () => void;
}) => (
  <button
    type="button"
    className="subagents-row"
    data-status={view.status}
    aria-label={`打开子智能体任务：${view.title}`}
    onClick={onOpen}
  >
    <AgentGlyph view={view} />
    <span className="subagents-row-copy">
      <span className="subagents-row-title">{view.title}</span>
      <span className="subagents-row-meta">
        <span className="subagents-row-status">{statusText(view)}</span>
        {view.summary && view.summary !== view.title && (
          <span className="subagents-row-summary">{summaryText(view)}</span>
        )}
        {view.status === "completed" && view.relativeTimeLabel && (
          <span className="subagents-row-time">{relativeTimeText(view.relativeTimeLabel)}</span>
        )}
      </span>
    </span>
    <ChevronRight className="subagents-row-chevron" size={14} aria-hidden="true" />
  </button>
);

const AgentDetail = ({
  view,
  onBack,
  onFetchResult,
  onStop,
  transcriptMessages,
  workspaceRoot,
  conversationId,
  transcriptLoading,
  transcriptError,
  onRefreshTranscript,
  pendingAction,
}: {
  view: AgentView;
  onBack: () => void;
  onFetchResult: () => void;
  onStop: () => void;
  transcriptMessages: ChatMessage[];
  workspaceRoot: string;
  /** The parent conversation owns the child journal's persisted artifacts. */
  conversationId?: string;
  transcriptLoading: boolean;
  transcriptError: string;
  onRefreshTranscript: () => void;
  pendingAction: "stop" | "result" | null;
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
          <strong>{view.title}</strong>
          <span>{[statusText(view), relativeTimeText(view.relativeTimeLabel)].filter(Boolean).join(" · ")}</span>
        </span>
        <span className="subagents-detail-header-actions">
          <button
            type="button"
            className="subagents-detail-icon-action"
            aria-label="刷新子智能体工作详情"
            title="刷新工作详情"
            onClick={onRefreshTranscript}
            disabled={transcriptLoading}
          >
            <RefreshCw size={14} className={transcriptLoading ? "is-spinning" : undefined} />
          </button>
          {view.canStop && (
            <button
              type="button"
              className="subagents-detail-icon-action subagents-detail-stop"
              aria-label={pendingAction === "stop" ? "正在停止子智能体" : "停止子智能体"}
              title="停止子智能体"
              onClick={onStop}
              disabled={pendingAction != null}
            >
              <Square size={13} />
            </button>
          )}
        </span>
      </header>

      <div className="subagents-detail-body">
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
            {isLive ? "子智能体正在启动，工作记录会实时显示在这里。" : "这个子智能体没有可回放的工作记录。"}
          </div>
        )}
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
      </div>
    </section>
  );
};

export const SubagentsTab = () => {
  const subagents = useAppStore((state) => state.subagents);
  const selectedAgentId = useAppStore((state) => state.focusedSubagentId);
  const setSelectedAgentId = useAppStore((state) => state.setFocusedSubagentId);
  const [showAllCompleted, setShowAllCompleted] = useState(false);
  const [pendingAction, setPendingAction] = useState<{ id: string; kind: "stop" | "result" } | null>(null);
  const [transcriptPresentation, setTranscriptPresentation] = useState<TranscriptPresentation>(
    emptyTranscriptPresentation,
  );
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState("");
  const transcriptRequestRef = useRef(0);
  const transcriptPresentationRef = useRef<TranscriptPresentation>(emptyTranscriptPresentation());
  const views = projectAgentViews(subagents);
  const selectedView = views.find((view) => view.id === selectedAgentId);
  const selectedAgent = subagents.find((agent) => agent.id === selectedAgentId);
  const conversationId = useAppStore((state) => state.conversationId);
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const visibleTranscript = transcriptPresentation.ownerId === selectedAgentId
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
        || useAppStore.getState().focusedSubagentId !== id
      ) return;
      const resultLevel = String(result.level || "").toLowerCase();
      if (!commandResultSucceeded(result) || resultLevel === "warning") {
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
        && Number.isFinite(responseSeq)
        && (
          responseSeq < currentPresentation.seq
          || (responseSeq === currentPresentation.seq && currentPresentation.source === "push")
        )
      ) return;
      const nextSeq = Number.isFinite(responseSeq) ? responseSeq : 0;
      const hydrated = hydrateMessages(rawMessages);
      commitTranscriptPresentation({
        ownerId: id,
        messages: hydrated,
        seq: nextSeq,
        source: "durable",
      });
    } catch (error) {
      if (requestId !== transcriptRequestRef.current) return;
      setTranscriptError(error instanceof Error ? error.message : "无法读取子智能体工作记录。");
    } finally {
      if (requestId === transcriptRequestRef.current) setTranscriptLoading(false);
    }
  }, [conversationId, workingDirectory, commitTranscriptPresentation]);

  useEffect(() => {
    setPendingAction(null);
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
      && selectedAgent.transcriptSeq <= currentPresentation.seq
    ) return;
    transcriptRequestRef.current += 1;
    setTranscriptLoading(false);
    setTranscriptError("");
    commitTranscriptPresentation({
      ownerId: selectedAgentId,
      messages: pushedMessages,
      seq: selectedAgent.transcriptSeq,
      source: "push",
    });
  }, [
    selectedAgent?.transcriptMessages,
    selectedAgent?.transcriptSeq,
    selectedAgentId,
    commitTranscriptPresentation,
  ]);

  const stop = async (id: string) => {
    if (!conversationId) return;
    if (pendingAction?.id === id) return;
    const ownerConversationId = conversationId;
    setPendingAction({ id, kind: "stop" });
    try {
      const result = await sendClientCommandAwaitResult({
        type: "subagent.cancel",
        subagent_id: id,
        conversation_id: conversationId,
        workspace_root: workingDirectory || undefined,
      }, "subagent.cancel");
      if (useAppStore.getState().conversationId !== ownerConversationId) return;
      if (!commandResultSucceeded(result)) {
        pushToast(result.message || "停止子智能体失败。", "error", 4000);
      } else if (String(result.level || "").toLowerCase() === "warning" && result.message) {
        pushToast(result.message, "warning", 3500);
      }
    } catch (error) {
      pushToast(error instanceof Error ? error.message : "停止子智能体失败。", "error", 4000);
    } finally {
      setPendingAction((current) => current?.id === id && current.kind === "stop" ? null : current);
    }
  };

  const fetchResult = async (id: string) => {
    if (!conversationId) return;
    if (pendingAction?.id === id) return;
    const ownerConversationId = conversationId;
    setPendingAction({ id, kind: "result" });
    try {
      const result = await sendClientCommandAwaitResult({
        type: "subagent.status",
        subagent_id: id,
        include_result: true,
        conversation_id: conversationId,
        workspace_root: workingDirectory || undefined,
      }, "subagent.status");
      if (useAppStore.getState().conversationId !== ownerConversationId) return;
      if (!commandResultSucceeded(result)) {
        pushToast(result.message || "获取子智能体结果失败。", "error", 4000);
      } else if (String(result.level || "").toLowerCase() === "warning" && result.message) {
        pushToast(result.message, "warning", 3500);
      }
    } catch (error) {
      pushToast(error instanceof Error ? error.message : "获取子智能体结果失败。", "error", 4000);
    } finally {
      setPendingAction((current) => current?.id === id && current.kind === "result" ? null : current);
    }
  };

  if (selectedView) {
    return (
      <AgentDetail
        key={`${conversationId}:${selectedView.id}`}
        view={selectedView}
        onBack={() => setSelectedAgentId(null)}
        onFetchResult={() => void fetchResult(selectedView.id)}
        onStop={() => void stop(selectedView.id)}
        transcriptMessages={visibleTranscript.messages}
        workspaceRoot={workingDirectory}
        conversationId={conversationId || undefined}
        transcriptLoading={transcriptLoading}
        transcriptError={transcriptError}
        onRefreshTranscript={() => void loadTranscript(selectedView.id)}
        pendingAction={pendingAction?.id === selectedView.id ? pendingAction.kind : null}
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
    { key: "active", label: "进行中", items: activeViews },
    { key: "attention", label: "需要处理", items: attentionViews },
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
                <span>{group.label}{group.key === "completed" ? ` · ${items.length}` : ""}</span>
                {group.key !== "completed" && <span>{items.length}</span>}
              </div>
              <div className="subagents-list">
                {visibleItems.map((view) => (
                  <AgentRow
                    key={view.id}
                    view={view}
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
