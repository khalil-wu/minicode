import { ChevronDown, ChevronUp } from "lucide-react";
import { useAppStore } from "../stores";
import { ToolCallTimeline } from "../chat/tool-calls/ToolCallTimeline";
import { GitPanel } from "../panels/GitPanel";

const TABS = [
  { id: "git", label: "Git" },
  { id: "timeline", label: "Timeline" },
  { id: "budget", label: "Budget" },
  { id: "debug", label: "Debug" },
] as const;

export const BottomDock = () => {
  const dockCollapsed = useAppStore((s) => s.dockCollapsed);
  const dockHeight = useAppStore((s) => s.dockHeight);
  const activeBottomTab = useAppStore((s) => s.activeBottomTab);
  const totalBudgetPercent = useAppStore((s) => s.totalBudgetPercent);
  const toolCount = useAppStore((s) => s.toolCallCount);
  const setActiveBottomTab = useAppStore((s) => s.setActiveBottomTab);
  const toggleDock = useAppStore((s) => s.toggleDock);
  const visibleTab = activeBottomTab === "terminal" || activeBottomTab === "tasks" ? "git" : activeBottomTab;
  return (
    <section
      className="flex flex-col shrink-0"
      style={{
        display: "flex",
        flexDirection: "column",
        flexShrink: 0,
        height: dockCollapsed ? 32 : dockHeight,
        background: "var(--surface-base)",
        borderTop: "1px solid var(--border-subtle)",
        transition: "height var(--transition-md, 200ms cubic-bezier(0.4,0,0.2,1))",
      }}
    >
      <div
        className="h-8 flex items-center px-2 gap-1"
        style={{
          height: 32,
          display: "flex",
          alignItems: "center",
          padding: "0 8px",
          gap: 4,
          borderBottom: dockCollapsed ? 0 : "1px solid var(--border-subtle)",
          background: "var(--surface-page)",
        }}
      >
        {TABS.map((t) => {
          let badge: string | null = null;
          if (t.id === "budget" && totalBudgetPercent > 0.7) badge = `${Math.round(totalBudgetPercent * 100)}%`;
          if (t.id === "timeline") {
            if (toolCount > 0) badge = String(toolCount);
          }

          return (
            <button
              key={t.id}
              onClick={() => {
                setActiveBottomTab(t.id);
                if (dockCollapsed) toggleDock();
              }}
              className="border-0 px-2.5 py-1 cursor-pointer inline-flex items-center gap-1"
              style={{
                background: visibleTab === t.id && !dockCollapsed ? "var(--surface-raised)" : "transparent",
                color: visibleTab === t.id ? "var(--text-primary)" : "var(--text-muted)",
                borderRadius: "var(--radius-sm, 4px)",
                fontSize: "var(--text-xs)",
              }}
            >
              {t.label}
              {badge && (
                <span className="rounded-full px-1.5 text-center font-semibold" style={{
                  fontSize: "var(--text-xs)",
                  fontFamily: "var(--font-mono)",
                  background: t.id === "budget" && totalBudgetPercent > 0.9 ? "var(--state-danger)" : "var(--surface-active)",
                  color: t.id === "budget" && totalBudgetPercent > 0.9 ? "var(--text-on-accent)" : "var(--text-secondary)",
                  lineHeight: "16px",
                  minWidth: 16,
                }}>
                  {badge}
                </span>
              )}
            </button>
          );
        })}
        <div className="flex-1" />
        <button
          onClick={toggleDock}
          aria-label={dockCollapsed ? "Expand bottom dock" : "Collapse bottom dock"}
          title={dockCollapsed ? "Expand bottom dock" : "Collapse bottom dock"}
          className="bg-transparent border-0 cursor-pointer inline-flex items-center justify-center w-7 h-6"
          style={{
            color: "var(--text-muted)",
          }}
        >
          {dockCollapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      </div>
      <div
        aria-hidden={dockCollapsed}
        className="flex-1 overflow-auto"
        style={{
          fontSize: "var(--text-sm)",
          background: "var(--surface-base)",
          opacity: dockCollapsed ? 0 : 1,
          transform: dockCollapsed ? "translateY(6px)" : "translateY(0)",
          pointerEvents: dockCollapsed ? "none" : "auto",
          transition: "opacity 140ms ease, transform var(--transition-md, 200ms cubic-bezier(0.4,0,0.2,1))",
        }}
      >
          {visibleTab === "timeline" && <div className="p-3"><ToolCallTimeline /></div>}
          {visibleTab === "budget" && (
            <div className="p-3">
              <div className="mb-2" style={{ color: "var(--text-secondary)" }}>
                Total: {(totalBudgetPercent * 100).toFixed(1)}%
              </div>
              <BudgetBars />
            </div>
          )}
          {visibleTab === "git" && <GitPanel />}
          {visibleTab === "debug" && <div className="p-3"><DebugLog /></div>}
      </div>
    </section>
  );
};

const BudgetBars = () => {
  const budgetBuckets = useAppStore((s) => s.budgetBuckets);
  if (budgetBuckets.length === 0)
    return <span style={{ color: "var(--text-muted)" }}>No budget data yet.</span>;
  return (
    <div className="grid grid-cols-1 gap-1.5">
      {budgetBuckets.map((b) => {
        const pct = b.limit > 0 ? b.used / b.limit : 0;
        return (
          <div key={b.name}>
            <div className="flex justify-between" style={{ fontSize: "var(--text-xs)" }}>
              <span style={{ color: "var(--text-secondary)" }}>{b.name}</span>
              <span style={{ color: "var(--text-muted)" }}>
                {b.used} / {b.limit}
              </span>
            </div>
            <div
              className="h-1 rounded-sm overflow-hidden"
              style={{
                background: "var(--surface-soft)",
              }}
            >
              <div
                className="h-full"
                style={{
                  width: `${Math.min(100, pct * 100)}%`,
                  background:
                    pct > 0.9
                      ? "var(--state-danger)"
                      : pct > 0.75
                        ? "var(--state-warning)"
                        : "var(--accent-primary)",
                }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
};

const DebugLog = () => {
  const messages = useAppStore((s) => s.messages);
  const toolCallCount = useAppStore((s) => s.toolCallCount);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const isConnected = useAppStore((s) => s.isConnected);
  return (
    <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
      <div>connected: {String(isConnected)}</div>
      <div>streaming: {String(isStreaming)}</div>
      <div>messages: {messages.length}</div>
      <div>tool_calls: {toolCallCount}</div>
    </div>
  );
};
