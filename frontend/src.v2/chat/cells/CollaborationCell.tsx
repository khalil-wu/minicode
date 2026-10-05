import { memo, useEffect, useState } from "react";
import { Bot, ChevronDown, ChevronRight } from "lucide-react";
import { AgentAvatar } from "../../components/AgentAvatar";
import { agentStatusSummary, projectAgentViews } from "../../lib/agent-view-model";
import type { CollaborationCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import { safeJsonParse } from "../../lib/safe-parse";
import { addInspectorPayload } from "../inspectorEntries";
import "./cells.css";
import "./CollaborationCell.css";

export const CollaborationCell = memo(function CollaborationCell({
  cell,
  conversationId,
}: {
  cell: CollaborationCellState;
  conversationId?: string;
}) {
  const needsAttention = cell.status === "failed" || cell.status === "partial";
  const [expanded, setExpanded] = useState(!cell.collapsed || needsAttention);

  useEffect(() => {
    setExpanded(!cell.collapsed || needsAttention);
  }, [cell.id, cell.collapsed, needsAttention]);

  const agentIds = [...new Set(cell.entries.map((entry) => entry.agentId))];
  const agents = useAppStore((state) => conversationId
    ? conversationId === state.conversationId ? state.subagents : state.conversationAgentStates[conversationId]?.subagents
    : undefined);
  const activeConversationId = useAppStore((state) => state.conversationId);
  const views = projectAgentViews(agents ?? []);
  const agentFor = (id: string) => {
    const exact = views.find((agent) => agent.id === id || agent.identityKey === id);
    if (exact) return exact;
    const named = views.filter((agent) => agent.teammateName === id);
    return named.length === 1 ? named[0] : undefined;
  };
  const involvedAgents = views.filter((view) => agentIds.some((id) => agentFor(id)?.id === view.id));
  const opaqueIdentity = /^(?:(?:subagent|agent|thread|call|session|run)[-_:][\w-]+|[a-f0-9]{8,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i;
  const agentNames = new Map(agentIds.map((id, index) => {
    const label = cell.entries.find((entry) => entry.agentId === id)!.agentLabel;
    const labelIsId = cell.action !== "delegated" && opaqueIdentity.test(label);
    const agent = agentFor(id);
    const name = agent ? agent.teammateName || agent.title : labelIsId || label === "Agent" || label === "子智能体"
      ? `子任务 ${index + 1}`
      : label;
    return [id, name] as const;
  }));
  const displayMessage = (content: string) => {
    const protocol = cell.action === "sent_message" && content.trim().startsWith("{")
      ? safeJsonParse<{ type?: string; reason?: string; summary?: string } | null>(content, null)
      : null;
    const isProtocol = ["idle_notification", "shutdown_request", "shutdown_response", "plan_approval_request", "plan_approval_response", "permission_request", "permission_response"].includes(protocol?.type ?? "");
    return isProtocol ? protocol?.reason || protocol?.summary || "" : content;
  };
  const actionLabel = cell.status === "cancelled" ? "已取消"
    : cell.status === "partial" ? "部分完成"
    : cell.action === "delegated"
      ? cell.status === "running" ? "正在委派" : cell.status === "success" ? cell.background ? "已在后台启动" : "已委派" : "委派失败"
    : cell.action === "closed"
    ? cell.status === "running" ? "正在停止" : cell.status === "success" ? "已停止" : "停止失败"
    : cell.status === "running" ? "正在发送" : cell.status === "success" ? "已发送消息" : "发送失败";
  const summary = agentIds.length > 0 ? `${actionLabel} · ${agentIds.length} 个子任务` : actionLabel;
  const canExpand = cell.entries.length > 0;

  return (
    <div className="collaboration-cell" data-action={cell.action} data-status={cell.status}>
      <button
        type="button"
        className="collaboration-cell-summary"
        aria-label={canExpand ? `${expanded ? "收起" : "展开"}${summary}详情` : summary}
        aria-expanded={canExpand ? expanded : undefined}
        disabled={!canExpand}
        onClick={() => { if (canExpand) setExpanded((value) => !value); }}
      >
        {involvedAgents.length > 0 ? <span className="collaboration-cell-avatars" aria-hidden="true">
          {involvedAgents.slice(0, 3).map((agent) => <AgentAvatar key={agent.id} identityKey={agent.identityKey} status={agent.status} size="small" />)}
        </span> : <Bot size={15} strokeWidth={1.8} aria-hidden="true" />}
        <span>{summary}</span>
        {cell.action === "delegated" && involvedAgents.length > 0 && <span className="collaboration-cell-live-summary">{agentStatusSummary(involvedAgents)}</span>}
        {canExpand && (
          <span className="collaboration-cell-chevron" aria-hidden="true">
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </span>
        )}
      </button>

      {expanded && (
        <div className="collaboration-cell-details">
          {cell.entries.filter((entry, index, entries) => entries.findIndex((candidate) =>
            candidate.agentId === entry.agentId && candidate.content === entry.content) === index).map((entry, index) => {
            const agentLabel = agentNames.get(entry.agentId);
            const agent = agentFor(entry.agentId);
            const canOpen = Boolean(agent && conversationId === activeConversationId);
            const content = displayMessage(entry.content || "");
            return (
              <div key={`${entry.agentId}-${index}`}>
                <div className="collaboration-cell-detail">
                  {canOpen && agent ? <button type="button" className="collaboration-agent-link" aria-label={`打开子智能体：${agentLabel}`} onClick={() => {
                    const store = useAppStore.getState();
                    store.setFocusedSubagentId(agent.id);
                    store.setRightStackTab("subagents");
                  }}>
                    <AgentAvatar identityKey={agent.identityKey} status={agent.status} size="small" />
                    <strong>{agentLabel}</strong>
                    <span className="collaboration-agent-status">{agent.statusLabel}</span>
                    <ChevronRight size={12} aria-hidden="true" />
                  </button> : <strong>{agentLabel}</strong>}
                  {content && content !== agentLabel && (
                    cell.action === "delegated" ? <details className="collaboration-task-instructions">
                      <summary><ChevronRight size={12} aria-hidden="true" />委派指令</summary>
                      <div className="collaboration-cell-message">{content}</div>
                    </details> : <span className="collaboration-cell-message">{content}</span>
                  )}
                </div>
              </div>
            );
          })}
          <button type="button" className="collaboration-cell-summary" onClick={() => {
            addInspectorPayload("tool_call", cell.id, { kind: "collaboration", conversation_id: conversationId, cell });
            const store = useAppStore.getState();
            store.setInspectorFocus({ kind: "tool_call", id: cell.id });
            store.setRightStackTab("inspector");
          }}>查看运行详情</button>
        </div>
      )}
      {cell.error && <div role="alert" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{cell.error}</div>}
    </div>
  );
});
