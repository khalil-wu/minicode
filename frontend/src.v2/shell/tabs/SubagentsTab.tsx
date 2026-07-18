/**
 * Calm, user-facing view of delegated work.
 *
 * Protocol records, workflow containers and runtime diagnostics belong in
 * Inspector. This panel only answers: what is being worked on, where it stands,
 * and what result is available.
 */
import { ArrowLeft, ChevronDown, ChevronRight, RotateCcw, Send, Square } from "lucide-react";
import { useState } from "react";
import { MarkdownRenderer } from "../../chat/messages/MarkdownRenderer";
import {
  projectAgentViews,
  hasCompletedAssistantReply,
  type AgentDisplayStatus,
  type AgentView,
} from "../../lib/agent-view-model";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
import type { SubagentMessageState } from "../../stores/types";
import { EmptyLine, PanelHeader, SmallButton, StatusMark } from "../SidebarShared";
import "./SubagentsTab.css";

const COMPLETED_PREVIEW_LIMIT = 6;

const GROUPS: Array<{
  status: AgentDisplayStatus;
  label: string;
}> = [
  { status: "attention", label: "需要处理" },
  { status: "running", label: "正在处理" },
  { status: "waiting", label: "等待中" },
  { status: "completed", label: "已完成" },
];

const statusMarkValue = (view: AgentView): string => {
  if (view.status === "completed") return "done";
  if (view.effectiveStatus === "partial" || view.effectiveStatus === "cancelled") {
    return "blocked";
  }
  return view.effectiveStatus;
};

const AgentRow = ({
  view,
  onOpen,
  showStatus = true,
}: {
  view: AgentView;
  onOpen: () => void;
  showStatus?: boolean;
}) => (
  <button
    type="button"
    className="subagents-row"
    data-status={view.status}
    aria-label={`打开任务：${view.title}`}
    onClick={onOpen}
  >
    <StatusMark status={statusMarkValue(view)} />
    <span className="subagents-row-copy">
      <span className="subagents-row-title">{view.title}</span>
      <span className="subagents-row-summary">
        {view.executionMode === "background" ? "后台继续，不阻塞主回答" : "主回答会等待此任务"}
      </span>
      {view.summary && view.summary !== view.title && (
        <span className="subagents-row-summary">{view.summary}</span>
      )}
    </span>
    {showStatus && <span className="subagents-row-status">{view.statusLabel}</span>}
    <ChevronRight className="subagents-row-chevron" size={14} aria-hidden="true" />
  </button>
);

const AgentDetail = ({
  view,
  onBack,
  onFetchResult,
  onStop,
  onResume,
  messages,
  canSteer,
  onSendMessage,
}: {
  view: AgentView;
  onBack: () => void;
  onFetchResult: () => void;
  onStop: () => void;
  onResume: () => void;
  messages: SubagentMessageState[];
  canSteer: boolean;
  onSendMessage: (message: string) => void;
}) => {
  const [messageDraft, setMessageDraft] = useState("");
  const hasResult = !view.handledByParent && Boolean(view.resultContent || view.resultError);
  const showSummary = !view.handledByParent
    && Boolean(view.summary && view.summary !== view.title)
    && (view.status === "running" || !hasResult);

  return (
    <section className="subagents-detail" aria-label={`任务详情：${view.title}`}>
      <header className="subagents-detail-header">
        <button
          type="button"
          className="subagents-back"
          aria-label="返回子智能体列表"
          onClick={onBack}
        >
          <ArrowLeft size={16} />
        </button>
        <StatusMark status={statusMarkValue(view)} />
        <span className="subagents-detail-heading">
          <strong>{view.title}</strong>
          <span>{view.statusLabel}</span>
          <span>{view.executionMode === "background" ? "后台任务" : "主回答依赖"}</span>
        </span>
      </header>

      <div className="subagents-detail-body">
        {showSummary && (
          <section className="subagents-detail-progress" aria-label="任务进展">
            <h3>{view.status === "running" ? "当前进展" : "任务摘要"}</h3>
            <p>{view.summary}</p>
          </section>
        )}

        {!view.handledByParent && view.activityLog.length > 0 && (
          <section className="subagents-detail-timeline" aria-label="执行过程">
            <h3>执行过程</h3>
            <ol>
              {view.activityLog.map((activity, index) => (
                <li key={`${index}-${activity}`}>{activity}</li>
              ))}
            </ol>
          </section>
        )}

        {!view.handledByParent && (messages.length > 0 || canSteer) && (
          <section className="subagents-detail-messages" aria-label="协作消息">
            <h3>协作消息</h3>
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
                  placeholder="给这个子智能体补充指令…"
                  aria-label="给子智能体发送消息"
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

        {hasResult && (
          <section className="subagents-detail-result" aria-label="任务结果">
            <h3>结果</h3>
            {view.resultError && (
              <div className="subagents-detail-error">{view.resultError}</div>
            )}
            {view.resultContent && <MarkdownRenderer content={view.resultContent} />}
          </section>
        )}

        {!view.handledByParent && view.needsResult && !hasResult && (
          <p className="subagents-result-ready">结果已就绪，可以获取查看。</p>
        )}

        <div className="subagents-detail-actions">
          {!view.handledByParent && view.needsResult && (
            <SmallButton
              icon={<ChevronRight size={12} />}
              label="获取结果"
              onClick={onFetchResult}
            />
          )}
          {!view.handledByParent && view.canStop && (
            <SmallButton
              icon={<Square size={12} />}
              label="停止任务"
              onClick={onStop}
            />
          )}
          {!view.handledByParent && view.canResume && (
            <SmallButton
              icon={<RotateCcw size={12} />}
              label={view.effectiveStatus === "partial" ? "继续任务" : "重试任务"}
              onClick={onResume}
            />
          )}
        </div>
      </div>
    </section>
  );
};

export const SubagentsTab = () => {
  const subagents = useAppStore((state) => state.subagents);
  const messages = useAppStore((state) => state.messages);
  const isStreaming = useAppStore((state) => state.isStreaming);
  const selectedAgentId = useAppStore((state) => state.focusedSubagentId);
  const setSelectedAgentId = useAppStore((state) => state.setFocusedSubagentId);
  const [showAllCompleted, setShowAllCompleted] = useState(false);
  const parentCompleted = !isStreaming && hasCompletedAssistantReply(messages);
  const views = projectAgentViews(subagents, Date.now(), { parentCompleted });
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

  const resume = (id: string) => {
    sendClientCommand({ type: "subagent.resume", subagent_id: id });
  };

  if (selectedView) {
    return (
      <AgentDetail
        view={selectedView}
        onBack={() => setSelectedAgentId(null)}
        onFetchResult={() => fetchResult(selectedView.id)}
        onStop={() => stop(selectedView.id)}
        onResume={() => resume(selectedView.id)}
        messages={selectedAgent?.messages ?? []}
        canSteer={Boolean(selectedAgent && ["pending", "running", "blocked"].includes(selectedAgent.status))}
        onSendMessage={(message) => sendMessage(selectedView.id, message)}
      />
    );
  }

  if (views.length === 0) {
    return (
      <div className="subagents-tab">
        <PanelHeader title="子智能体" meta="" />
        <div className="subagents-empty">
          <EmptyLine>没有正在执行的子智能体</EmptyLine>
          <span>需要分工时，进展会显示在这里。</span>
        </div>
      </div>
    );
  }

  return (
    <div className="subagents-tab">
      <PanelHeader title="子智能体" meta="" />
      <div className="subagents-groups">
        {GROUPS.map((group) => {
          const items = views.filter((view) => view.status === group.status);
          if (items.length === 0) return null;
          const canCollapse = group.status === "completed" && items.length > COMPLETED_PREVIEW_LIMIT;
          const visibleItems = canCollapse && !showAllCompleted
            ? items.slice(0, COMPLETED_PREVIEW_LIMIT)
            : items;

          return (
            <section
              key={group.status}
              className="subagents-group"
              aria-label={`${group.label}，${items.length} 项`}
            >
              <div className="subagents-group-heading">
                <span>{group.label}</span>
                <span>{items.length}</span>
              </div>
              <div className="subagents-list">
                {visibleItems.map((view) => (
                  <AgentRow
                    key={view.id}
                    view={view}
                    showStatus={group.status !== "completed" || view.statusLabel !== "已完成"}
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
                  {showAllCompleted ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  <span>
                    {showAllCompleted
                      ? "收起已完成任务"
                      : `查看其余 ${items.length - COMPLETED_PREVIEW_LIMIT} 项`}
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
