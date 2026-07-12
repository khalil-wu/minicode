/**
 * Calm, user-facing view of delegated work.
 *
 * Protocol records, workflow containers and runtime diagnostics belong in
 * Inspector. This panel only answers: what is being worked on, where it stands,
 * and what result is available.
 */
import { ArrowLeft, ChevronDown, ChevronRight, Square } from "lucide-react";
import { useState } from "react";
import { MarkdownRenderer } from "../../chat/messages/MarkdownRenderer";
import {
  projectAgentViews,
  type AgentDisplayStatus,
  type AgentView,
} from "../../lib/agent-view-model";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { useAppStore } from "../../stores";
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
}: {
  view: AgentView;
  onBack: () => void;
  onFetchResult: () => void;
  onStop: () => void;
}) => {
  const hasResult = Boolean(view.resultContent || view.resultError);
  const showSummary = Boolean(view.summary && view.summary !== view.title);

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
        </span>
      </header>

      <div className="subagents-detail-body">
        {showSummary && (
          <section className="subagents-detail-progress" aria-label="任务进展">
            <h3>{view.status === "running" ? "当前进展" : "任务摘要"}</h3>
            <p>{view.summary}</p>
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

        {view.needsResult && !hasResult && (
          <p className="subagents-result-ready">结果已就绪，可以获取查看。</p>
        )}

        <div className="subagents-detail-actions">
          {view.needsResult && (
            <SmallButton
              icon={<ChevronRight size={12} />}
              label="获取结果"
              onClick={onFetchResult}
            />
          )}
          {view.canStop && (
            <SmallButton
              icon={<Square size={12} />}
              label="停止任务"
              onClick={onStop}
            />
          )}
        </div>
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

  if (selectedView) {
    return (
      <AgentDetail
        view={selectedView}
        onBack={() => setSelectedAgentId(null)}
        onFetchResult={() => fetchResult(selectedView.id)}
        onStop={() => stop(selectedView.id)}
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
