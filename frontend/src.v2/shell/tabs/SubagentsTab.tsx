/**
 * Calm, user-facing view of delegated work.
 *
 * Protocol records and runtime diagnostics belong in
 * Inspector. This panel only answers: what is being worked on, where it stands,
 * and what result is available.
 */
import { ArrowLeft, Bot, ChevronDown, ChevronRight, Send, Square } from "lucide-react";
import { useState } from "react";
import { MarkdownRenderer } from "../../chat/messages/MarkdownRenderer";
import { AgentAvatar } from "../../components/AgentAvatar";
import {
  projectAgentViews,
  type AgentView,
} from "../../lib/agent-view-model";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
import type { SubagentMessageState } from "../../stores/types";
import { EmptyLine, SmallButton } from "../SidebarShared";
import "./SubagentsTab.css";

const COMPLETED_PREVIEW_LIMIT = 6;

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
  messages,
  canSteer,
  onSendMessage,
}: {
  view: AgentView;
  onBack: () => void;
  onFetchResult: () => void;
  onStop: () => void;
  messages: SubagentMessageState[];
  canSteer: boolean;
  onSendMessage: (message: string) => void;
}) => {
  const [messageDraft, setMessageDraft] = useState("");
  const hasResult = Boolean(view.resultContent || view.resultError);
  const showSummary = Boolean(view.summary && view.summary !== view.title)
    && (view.status === "running" || !hasResult);

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
      </header>

      <div className="subagents-detail-body">
        {hasResult && (
          <section className="subagents-detail-result" aria-label="任务结果">
            {view.resultError && (
              <div className="subagents-detail-error">{view.resultError}</div>
            )}
            {view.resultContent && <MarkdownRenderer content={view.resultContent} />}
          </section>
        )}

        {showSummary && <p className="subagents-detail-lead">{view.summary}</p>}

        {view.needsResult && !hasResult && (
          <p className="subagents-result-ready">结果已准备好，可以获取。</p>
        )}

        {(view.activityLog.length > 0 || messages.length > 0 || canSteer || view.needsResult || view.canStop) && (
          <details className="subagents-runtime-details">
            <summary>运行详情</summary>
            <div className="subagents-runtime-details-body">
              {view.activityLog.length > 0 && (
                <section className="subagents-detail-timeline" aria-label="时间线">
                  <h3>时间线</h3>
                  <ol>
                    {view.activityLog.map((activity, index) => (
                      <li key={`${index}-${activity}`}>{activity}</li>
                    ))}
                  </ol>
                </section>
              )}

              {(messages.length > 0 || canSteer) && (
                <section className="subagents-detail-messages" aria-label="协作消息">
                  <h3>消息</h3>
                  {messages.length > 0 && (
                    <ol>
                      {messages.map((message) => (
                        <li key={message.messageId} data-sender={message.senderId === "user" ? "user" : "agent"}>
                          <span>{message.senderId === "user" ? "你" : view.title}</span>
                          <p>{message.content}</p>
                          {message.deliveryStatus === "sending" && <small>发送中…</small>}
                          {message.deliveryStatus === "failed" && <small>发送失败</small>}
                        </li>
                      ))}
                    </ol>
                  )}
                  {canSteer && (
                    <form
                      className="subagents-message-composer"
                      onSubmit={(event) => {
                        event.preventDefault();
                        const value = messageDraft.trim();
                        if (!value) return;
                        onSendMessage(value);
                        setMessageDraft("");
                      }}
                    >
                      <textarea
                        value={messageDraft}
                        onChange={(event) => setMessageDraft(event.target.value)}
                        placeholder="继续给这个子智能体补充指令…"
                        aria-label="给这个子智能体发送消息"
                        rows={2}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                            event.preventDefault();
                            event.currentTarget.form?.requestSubmit();
                          }
                        }}
                      />
                      <button type="submit" disabled={!messageDraft.trim()} aria-label="发送给子智能体">
                        <Send size={14} />
                      </button>
                    </form>
                  )}
                </section>
              )}

              <div className="subagents-detail-actions">
                {view.needsResult && (
                  <SmallButton icon={<ChevronRight size={14} />} label="获取结果" onClick={onFetchResult} />
                )}
                {view.canStop && (
                  <SmallButton icon={<Square size={14} />} label="停止任务" onClick={onStop} />
                )}
              </div>
            </div>
          </details>
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
  const views = projectAgentViews(subagents);
  const selectedView = views.find((view) => view.id === selectedAgentId);
  const selectedAgent = subagents.find((agent) => agent.id === selectedAgentId);
  const conversationId = useAppStore((state) => state.conversationId);

  const stop = (id: string) => {
    sendClientCommand({ type: "subagent.cancel", subagent_id: id });
  };

  const fetchResult = (id: string) => {
    sendClientCommand({
      type: "subagent.status",
      subagent_id: id,
      include_result: true,
    });
  };

  const sendMessage = (id: string, message: string) => {
    const target = subagents.find((agent) => agent.id === id);
    const messageId = globalThis.crypto?.randomUUID?.() ?? `msg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
    const optimistic: SubagentMessageState = {
      messageId,
      senderId: "user",
      recipientId: id,
      content: message,
      createdAt: Date.now(),
      deliveryStatus: "sending",
    };
    useAppStore.getState().updateSubagent(id, {
      messages: [...(target?.messages ?? []), optimistic].slice(-100),
    }, conversationId ?? undefined);
    sendClientCommand({
      type: "send_message",
      recipient: id,
      sender: "user",
      message,
      message_id: messageId,
      conversation_id: conversationId ?? undefined,
      task_id: target?.taskId,
    });
  };

  if (selectedView) {
    return (
      <AgentDetail
        view={selectedView}
        onBack={() => setSelectedAgentId(null)}
        onFetchResult={() => fetchResult(selectedView.id)}
        onStop={() => stop(selectedView.id)}
        messages={selectedAgent?.messages ?? []}
        canSteer={Boolean(selectedAgent && ["pending", "running", "blocked"].includes(selectedAgent.status))}
        onSendMessage={(message) => sendMessage(selectedView.id, message)}
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
                      fetchResult(view.id);
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
