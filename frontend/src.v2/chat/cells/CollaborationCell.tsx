import { memo, useEffect, useState } from "react";
import { Bot, ChevronDown, ChevronRight } from "lucide-react";
import type { CollaborationCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import { safeJsonParse } from "../../lib/safe-parse";
import { addInspectorPayload } from "../inspectorEntries";
import "./cells.css";

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
  const opaqueIdentity = /^(?:(?:subagent|agent|thread|call|session|run)[-_:][\w-]+|[a-f0-9]{8,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i;
  const agentNames = new Map(agentIds.map((id, index) => {
    const label = cell.entries.find((entry) => entry.agentId === id)!.agentLabel;
    const labelIsId = cell.action !== "delegated" && opaqueIdentity.test(label);
    const role = agents?.find((agent) => agent.id === id)?.role;
    const name = labelIsId || label === "Agent" || label === "子智能体"
      ? role && !opaqueIdentity.test(role) ? role : `Agent ${index + 1}`
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
  const actionLabel = cell.status === "cancelled" ? "Cancelled"
    : cell.status === "partial" ? "Partial"
    : cell.action === "delegated"
      ? cell.status === "running" ? "Delegating" : cell.status === "success" ? cell.background ? "Started in background" : "Delegated" : "Delegation failed"
    : cell.action === "closed"
    ? cell.status === "running" ? "Closing" : cell.status === "success" ? "Closed" : "Close failed"
    : cell.status === "running" ? "Sending" : cell.status === "success" ? "Sent message" : "Send failed";
  const summary = agentIds.length > 0 ? `${actionLabel} · ${agentIds.length} ${agentIds.length === 1 ? "agent" : "agents"}` : actionLabel;
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
        <Bot size={15} strokeWidth={1.8} aria-hidden="true" />
        <span>{summary}</span>
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
            const content = displayMessage(entry.content || "");
            return (
              <div key={`${entry.agentId}-${index}`}>
                <div className="collaboration-cell-detail">
                  <strong>{agentLabel}</strong>
                  {content && content !== agentLabel && (
                    <>
                      <span aria-hidden="true">：</span>
                      <span className="collaboration-cell-message">{content}</span>
                    </>
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
          }}>技术诊断（Inspector）</button>
        </div>
      )}
      {cell.error && <div role="alert" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{cell.error}</div>}
    </div>
  );
});
