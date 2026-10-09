import { memo } from "react";
import { AgentAvatar } from "../../components/AgentAvatar";
import { projectAgentViews } from "../../lib/agent-view-model";
import type { CollaborationCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import "./cells.css";
import "./CollaborationCell.css";

export const CollaborationCell = memo(function CollaborationCell({
  cell,
  conversationId,
}: {
  cell: CollaborationCellState;
  conversationId?: string;
}) {
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
  const opaqueIdentity = /^(?:(?:subagent|agent|thread|call|session|run)[-_:][\w-]+|[a-f0-9]{8,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$/i;
  const agentNames = new Map(agentIds.map((id) => {
    const label = cell.entries.find((entry) => entry.agentId === id)!.agentLabel;
    const agent = agentFor(id);
    const namedLabel = label && !opaqueIdentity.test(label) && !["Agent", "子智能体", "子任务"].includes(label) ? label : "";
    const title = agent?.title && agent.title !== "子任务" ? agent.title : "";
    const identity = cell.entries.find((entry) => entry.agentId === id)?.agentIdentity;
    const name = id ? agent?.teammateName || title || namedLabel || identity?.split("/").filter(Boolean).at(-1) || id : "";
    return [id, name] as const;
  }));
  const openAgent = (id: string) => {
    const store = useAppStore.getState();
    store.setFocusedSubagentId(id);
    store.setRightStackTab("subagents");
  };

  if (cell.action !== "completed") {
    const started = cell.entries.filter((entry, index, entries) => entry.agentId.trim()
      && (agentFor(entry.agentId) || entry.agentStatus)
      && entries.findIndex((candidate) => candidate.agentId === entry.agentId) === index);
    return <div className="collaboration-cell collaboration-delegated" data-action={cell.action} data-status={cell.status}>
      {started.map((entry) => {
        const agent = agentFor(entry.agentId);
        const label = agentNames.get(entry.agentId);
        const canOpen = conversationId === activeConversationId;
        const hasStarted = cell.action === "delegated" && cell.status === "success"
          && (agent?.effectiveStatus || entry.agentStatus) !== "pending";
        const content = <><AgentAvatar identityKey={entry.agentIdentity || agent?.identityKey || entry.agentId} size="small" showStatus={false} />
          <span className="collaboration-delegated-label"><span className="collaboration-delegated-name">{label}</span>
            {hasStarted && <span className="collaboration-delegated-start">开始工作</span>}
          </span></>;
        return canOpen ? <button key={entry.agentId} type="button" className="collaboration-delegated-row"
          aria-label={`打开子智能体：${label}`} title={`查看 ${label} 的工作记录${agent ? ` · ${agent.statusLabel}` : ""}`}
          data-agent-status={agent?.effectiveStatus || entry.agentStatus} data-started={hasStarted} onClick={() => {
            if (!agent) useAppStore.getState().addSubagent({ id: entry.agentId, role: "subagent", status: entry.agentStatus!,
              teammateName: label, agentPath: entry.agentIdentity }, conversationId);
            openAgent(agent?.id ?? entry.agentId);
          }}>{content}</button>
          : <div key={entry.agentId} className="collaboration-delegated-row" data-agent-status={agent?.effectiveStatus || entry.agentStatus} data-started={hasStarted}>{content}</div>;
      })}
      {cell.error && <details className="collaboration-delegated-error">
        <summary>{cell.action === "delegated" ? "启动子智能体" : cell.action === "sent_message" ? "发送消息" : "停止子智能体"} · {cell.status === "cancelled" ? "已停止" : cell.status === "partial" ? "部分完成" : "失败"}</summary>
        <div role="alert">{cell.error}</div>
      </details>}
    </div>;
  }

  if (cell.action === "completed") {
    const statusLabel = cell.status === "failed" ? "失败" : cell.status === "partial" ? "部分完成" : cell.status === "cancelled" ? "已停止" : "已完成";
    return <div className="collaboration-completion" data-status={cell.status}>
      {cell.entries.map((entry) => {
        const agent = agentFor(entry.agentId);
        const label = agentNames.get(entry.agentId);
        const content = <><AgentAvatar identityKey={entry.agentIdentity || agent?.identityKey || entry.agentId} size="small" showStatus={false} />
          <span className="collaboration-completion-label"><span className="collaboration-completion-name">{label}</span>
            <span className="collaboration-completion-status">{statusLabel}</span>
          </span></>;
        const canOpen = entry.agentId.trim() && (agent || entry.agentStatus) && conversationId === activeConversationId;
        return canOpen
          ? <button key={entry.agentId} type="button" className="collaboration-completion-row" aria-label={`打开子智能体：${label}`}
            aria-controls="right-panel-drawer" title={`查看 ${label} 的工作记录`} onClick={() => {
              if (!agent) useAppStore.getState().addSubagent({ id: entry.agentId, role: "subagent", status: entry.agentStatus!,
                teammateName: entry.agentLabel, agentPath: entry.agentIdentity }, conversationId);
              openAgent(agent?.id ?? entry.agentId);
            }}>{content}</button>
          : <div key={entry.agentId} className="collaboration-completion-row">{content}</div>;
      })}
    </div>;
  }

});
