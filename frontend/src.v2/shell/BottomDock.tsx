import { Bug, Gauge, GitBranch, Plus, TerminalSquare, X } from "lucide-react";
import { lazy, Suspense } from "react";
import { useAppStore } from "../stores";
import { GitPanel } from "../panels/GitPanel";
import {
  promptCacheEffectivePromptTokens,
  promptCacheHitRate,
  promptCacheOrdinaryInputTokens,
} from "../chat/cacheUsage";
import { requestNewTerminalSession } from "../panels/terminalRequests";
import { ChunkErrorBoundary, SafeBoundary } from "./ChunkErrorBoundary";
import { PanelErrorFallback } from "../components/PanelErrorFallback";
import { PanelSkeleton } from "./PanelSkeleton";
import "./BottomDock.css";

const TABS = [
  { id: "terminal", label: "终端", icon: TerminalSquare },
  { id: "git", label: "Git", icon: GitBranch },
  { id: "budget", label: "用量", icon: Gauge },
  { id: "debug", label: "调试", icon: Bug },
] as const;

const LazyTerminalPanel = lazy(() =>
  import("../panels/TerminalPanel").then((module) => ({ default: module.TerminalPanel })),
);

export const BottomDock = () => {
  const dockCollapsed = useAppStore((s) => s.dockCollapsed);
  const dockHeight = useAppStore((s) => s.dockHeight);
  const activeBottomTab = useAppStore((s) => s.activeBottomTab);
  const totalBudgetPercent = useAppStore((s) => s.totalBudgetPercent);
  const openBottomTab = useAppStore((s) => s.openBottomTab);
  const closeBottomDock = useAppStore((s) => s.closeBottomDock);
  const setDockHeight = useAppStore((s) => s.setDockHeight);
  const visibleTab = activeBottomTab === "tasks" || activeBottomTab === "timeline" ? "terminal" : activeBottomTab;
  const isOpen = !dockCollapsed;

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    const pointerId = event.pointerId;
    const startY = event.clientY;
    const startHeight = dockHeight;
    const onMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== pointerId) return;
      setDockHeight(startHeight + startY - moveEvent.clientY);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      handle.removeEventListener("lostpointercapture", cleanup);
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    const onUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== pointerId) return;
      cleanup();
    };
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    handle.setPointerCapture(pointerId);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    handle.addEventListener("lostpointercapture", cleanup);
  };

  const handleResizeKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 20;
    let nextHeight: number | null = null;
    if (event.key === "ArrowUp") nextHeight = dockHeight + step;
    else if (event.key === "ArrowDown") nextHeight = dockHeight - step;
    else if (event.key === "Home") nextHeight = 180;
    else if (event.key === "End") nextHeight = 520;
    else if (event.key === "Enter") nextHeight = 240;
    if (nextHeight == null) return;
    event.preventDefault();
    setDockHeight(nextHeight);
  };

  return (
    <section
      className="mc-bottom-drawer"
      data-open={isOpen ? "true" : "false"}
      aria-label="底部工具"
      aria-hidden={!isOpen}
      style={{
        "--mc-bottom-drawer-height": `${dockHeight}px`,
      } as React.CSSProperties}
    >
      {isOpen && <div className="mc-bottom-drawer-resize" role="separator" aria-orientation="horizontal" aria-label="调整底部工具高度" aria-valuemin={180} aria-valuemax={520} aria-valuenow={Math.round(dockHeight)} tabIndex={0} onPointerDown={startResize} onKeyDown={handleResizeKeyDown} />}
      {isOpen && <div className="mc-bottom-drawer-header">
        <div role="tablist" aria-label="底部工具面板" style={{ display: "contents" }}>
        {TABS.map((t, index) => {
          const Icon = t.icon;
          let badge: string | null = null;
          if (t.id === "budget" && totalBudgetPercent > 0.7) badge = `${Math.round(totalBudgetPercent * 100)}%`;

          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`bottom-dock-tab-${t.id}`}
              aria-selected={visibleTab === t.id}
              aria-controls={`bottom-dock-panel-${t.id}`}
              tabIndex={visibleTab === t.id ? 0 : -1}
              onClick={() => openBottomTab(t.id)}
              onKeyDown={(event) => {
                let next = index;
                if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
                else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
                else if (event.key === "Home") next = 0;
                else if (event.key === "End") next = TABS.length - 1;
                else return;
                event.preventDefault();
                const target = TABS[next];
                openBottomTab(target.id);
                window.setTimeout(() => document.getElementById(`bottom-dock-tab-${target.id}`)?.focus(), 0);
              }}
              className="mc-bottom-drawer-tab"
              data-active={visibleTab === t.id ? "true" : "false"}
            >
              <Icon size={14} />
              {t.label}
              {badge && (
                <span className="mc-bottom-drawer-badge" data-danger={totalBudgetPercent > 0.9 ? "true" : "false"}>{badge}</span>
              )}
            </button>
          );
        })}
        </div>
        <div className="mc-bottom-drawer-spacer" />
        {visibleTab === "terminal" && (
          <button type="button" className="mc-icon-button" aria-label="新建终端" title="新建终端" onClick={requestNewTerminalSession}>
            <Plus size={15} />
          </button>
        )}
        <button type="button" className="mc-icon-button" aria-label="关闭底部工具" title="关闭底部工具" onClick={closeBottomDock}>
          <X size={15} />
        </button>
      </div>}
      <div
        className="mc-bottom-drawer-content"
        id={`bottom-dock-panel-${visibleTab}`}
        role="tabpanel"
        aria-labelledby={`bottom-dock-tab-${visibleTab}`}
      >
          {isOpen && visibleTab === "terminal" && (
            <ChunkErrorBoundary>
              <Suspense fallback={<PanelSkeleton kind="terminal" />}>
                <SafeBoundary fallback={<PanelErrorFallback panelName="终端" />}>
                  <LazyTerminalPanel />
                </SafeBoundary>
              </Suspense>
            </ChunkErrorBoundary>
          )}
          {isOpen && visibleTab === "budget" && (
            <div className="p-3">
              <div className="mb-2" style={{ color: "var(--text-secondary)", fontVariantNumeric: "tabular-nums" }}>
                总用量：{(totalBudgetPercent * 100).toFixed(1)}%
              </div>
              <PromptCacheStats />
              <BudgetBars />
            </div>
          )}
          {isOpen && visibleTab === "git" && <GitPanel />}
          {isOpen && visibleTab === "debug" && <div className="p-3"><DebugLog /></div>}
      </div>
    </section>
  );
};

const formatTokenCount = (value: number): string => {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
};

const PromptCacheStats = () => {
  const lastUsage = useAppStore((s) => s.lastUsage);
  const usageTotals = useAppStore((s) => s.usageTotals);
  if (!lastUsage && usageTotals.turns <= 0) return null;
  const last = lastUsage ?? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0 };
  const cacheRead = Math.max(0, last.cacheRead || 0);
  const cacheWrite = Math.max(0, last.cacheWrite || 0);
  const promptTotal = Math.max(0, promptCacheEffectivePromptTokens(last));
  const ordinaryInput = Math.max(0, promptCacheOrdinaryInputTokens(last));
  const totalCacheRead = Math.max(0, usageTotals.cacheRead || 0);
  const totalCacheWrite = Math.max(0, usageTotals.cacheWrite || 0);
  const totalPrompt = Math.max(0, promptCacheEffectivePromptTokens(usageTotals));
  const totalOrdinaryInput = Math.max(0, promptCacheOrdinaryInputTokens(usageTotals));
  const lastReasoning = Math.max(0, last.reasoning || 0);
  const totalReasoning = Math.max(0, usageTotals.reasoning || 0);
  const hasCache = cacheRead > 0 || cacheWrite > 0 || totalCacheRead > 0 || totalCacheWrite > 0;
  const hasPromptUsage = ordinaryInput > 0 || promptTotal > 0 || totalOrdinaryInput > 0 || totalPrompt > 0;
  const hasReasoning = lastReasoning > 0 || totalReasoning > 0;
  if (!hasCache && !hasPromptUsage && !hasReasoning) return null;
  const hitRate = promptCacheHitRate(last);
  const totalHitRate = promptCacheHitRate(usageTotals);
  const cacheText = hasPromptUsage || hasCache
    ? `普通输入 ${formatTokenCount(ordinaryInput)} · 缓存读取 ${formatTokenCount(cacheRead)} · 缓存写入 ${formatTokenCount(cacheWrite)} · 提示词总量 ${formatTokenCount(promptTotal)}`
      + ` · 命中 ${hitRate == null ? "n/a" : `${Math.round(hitRate)}%`}`
      + (usageTotals.turns > 1 ? ` · 会话提示词 ${formatTokenCount(totalPrompt)} / 命中 ${totalHitRate == null ? "n/a" : `${Math.round(totalHitRate)}%`} / ${usageTotals.turns} 轮` : "")
    : "";
  const reasoningText = hasReasoning
    ? `推理 ${formatTokenCount(lastReasoning)}`
      + (usageTotals.turns > 1 ? ` / 会话 ${formatTokenCount(totalReasoning)}` : "")
    : "";
  const details = [cacheText, reasoningText].filter(Boolean).join(" · ");
  return (
    <div style={cacheStatsStyle}>
      <div style={{ color: "var(--text-secondary)", fontWeight: "var(--fw-semibold)" }}>模型用量</div>
      <div style={{ color: "var(--text-muted)", textAlign: "right" }}>
        {details}
      </div>
    </div>
  );
};

const BudgetBars = () => {
  const budgetBuckets = useAppStore((s) => s.budgetBuckets);
  if (budgetBuckets.length === 0)
    return <span style={{ color: "var(--text-muted)" }}>暂无用量数据。</span>;
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

const cacheStatsStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 12,
  marginBottom: 10,
  padding: "8px 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  background: "var(--surface-soft)",
  fontSize: "var(--text-xs)",
};

const DebugLog = () => {
  const messages = useAppStore((s) => s.messages);
  const toolCallCount = useAppStore((s) => s.toolCallCount);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const isConnected = useAppStore((s) => s.isConnected);
  return (
    <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
      <div>连接：{String(isConnected)}</div>
      <div>流式输出：{String(isStreaming)}</div>
      <div>消息：{messages.length}</div>
      <div>工具调用：{toolCallCount}</div>
    </div>
  );
};
