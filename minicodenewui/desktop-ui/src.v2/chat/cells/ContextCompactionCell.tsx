import { useEffect, useState } from "react";
import { useAppStore } from "../../stores";
import { useI18n } from "../../i18n";
import { contextCompactionIcon } from "../../lib/context-compaction-icon";
import type { ContextCompactionCellState } from "./cellTypes";

/** Actual sites-end-resource xS/SS: a standalone status, with the 10s notice. */
export function ContextCompactionCell({ cell }: { cell: ContextCompactionCellState }) {
  const { t } = useI18n();
  const work = useAppStore(state => state.appMode === "cowork");
  const [delayed, setDelayed] = useState(() => Date.now() - cell.createdAt >= 10_000);
  useEffect(() => {
    if (cell.status !== "running" || delayed) return;
    const timer = window.setTimeout(() => setDelayed(true), Math.max(0, cell.createdAt + 10_000 - Date.now()));
    return () => window.clearTimeout(timer);
  }, [cell.status, cell.createdAt, delayed]);
  const title = cell.status === "running"
    ? work ? delayed ? "正在优化对话 · 可能需要几分钟" : "正在优化对话"
      : delayed ? "正在压缩上下文 · 可能需要几分钟" : "正在压缩上下文"
    : cell.status === "interrupted" ? work ? "优化已中断" : "压缩已中断"
      : cell.source === "manual" ? "上下文已压缩" : work ? "已优化对话" : "上下文已自动压缩";
  return <div className="context-compaction-cell" data-processing={cell.status === "running" || undefined}>
    <svg width={14} height={14} viewBox={contextCompactionIcon.viewBox} aria-hidden="true" fill="none">
      {contextCompactionIcon.paths.map((path, index) => <path key={index} d={path} fill="currentColor" />)}
    </svg>
    <span className={cell.status === "running" ? "agent-loop-timeline-group-live-title" : undefined}>{t(title)}</span>
  </div>;
}
