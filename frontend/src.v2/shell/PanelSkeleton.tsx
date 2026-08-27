import type { CSSProperties } from "react";
import type { PanelKind } from "../stores/types";

const PANEL_KIND_LABELS: Record<PanelKind, string> = {
  chat: "对话",
  diff: "Diff",
  editor: "编辑器",
  preview: "预览",
  terminal: "终端",
  plan: "计划",
  tasks: "任务",
  subagents: "子智能体",
  artifacts: "产物",
  inspector: "运行详情",
};

export const PanelSkeleton = ({ kind }: { kind: PanelKind }) => (
  <div style={shellStyle} aria-label={`正在加载${PANEL_KIND_LABELS[kind]}面板`}>
    <div style={toolbarStyle}>
      <span className="panel-skeleton-shimmer" style={{ ...barStyle, width: 82 }} />
      <span className="panel-skeleton-shimmer" style={{ ...barStyle, width: 38 }} />
      <span className="panel-skeleton-shimmer" style={{ ...barStyle, width: 52 }} />
    </div>
    <div style={bodyStyle}>
      <span className="panel-skeleton-shimmer" style={{ ...lineStyle, width: "62%" }} />
      <span className="panel-skeleton-shimmer" style={{ ...lineStyle, width: "78%" }} />
      <span className="panel-skeleton-shimmer" style={{ ...lineStyle, width: "46%" }} />
      <span className="panel-skeleton-shimmer" style={{ ...blockStyle, width: "100%", height: 92 }} />
      <span className="panel-skeleton-shimmer" style={{ ...lineStyle, width: "70%" }} />
    </div>
  </div>
);

const shimmer: CSSProperties = {
  background:
    "linear-gradient(90deg, var(--surface-soft), var(--surface-active), var(--surface-soft))",
  backgroundSize: "220% 100%",
};

const shellStyle: CSSProperties = {
  flex: 1,
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
  background: "var(--surface-base)",
};

const toolbarStyle: CSSProperties = {
  height: 34,
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "0 10px",
  borderBottom: "1px solid var(--border-subtle)",
};

const bodyStyle: CSSProperties = {
  flex: 1,
  display: "grid",
  alignContent: "start",
  gap: 10,
  padding: 14,
};

const barStyle: CSSProperties = {
  ...shimmer,
  height: 12,
  borderRadius: 4,
};

const lineStyle: CSSProperties = {
  ...shimmer,
  height: 10,
  borderRadius: 4,
};

const blockStyle: CSSProperties = {
  ...shimmer,
  borderRadius: 6,
};
