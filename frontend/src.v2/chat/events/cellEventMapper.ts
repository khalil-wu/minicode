/**
 * Cell event mapper — converts backend WebSocket events into
 * StatusNoticeCell and ErrorCell states.
 *
 * This sits alongside chatStreamEvents.ts but focuses exclusively on
 * producing cells that don't come from the assistant message's content
 * blocks (status changes, mode switches, interrupt notices, etc.).
 */

import type { ServerEvent } from "../../protocol/events";
import type {
  ErrorCellState,
  StatusNoticeCellState,
} from "../cells/cellTypes";

let _counter = 0;
function nextId(prefix: string): string {
  _counter += 1;
  return `${prefix}-${Date.now().toString(36)}-${_counter}`;
}

/**
 * Inspects a server event and returns zero or more cell states
 * that should be appended to the current turn.
 */
export function mapEventToCells(
  event: ServerEvent,
): (StatusNoticeCellState | ErrorCellState)[] {
  const now = Date.now();

  switch (event.type) {
    // ── Mode / Status Changes ──────────────────────────────────
    case "system_notice": {
      const ev = event as unknown as {
        kind?: string;
        title?: string;
        message?: string;
        tone?: StatusNoticeCellState["tone"];
      };
      return [
        {
          kind: "status_notice",
          id: nextId("notice"),
          tone: ev.tone ?? "info",
          title: ev.title ?? ev.message ?? "System notice",
          message: ev.message,
          createdAt: now,
        },
      ];
    }

    // ── Context Compaction ─────────────────────────────────────
    case "context_compacted": {
      const ev = event as unknown as { summary?: string };
      return [
        {
          kind: "status_notice",
          id: nextId("compact"),
          tone: "warning",
          title: "上下文已压缩",
          message: ev.summary || "已自动压缩对话历史以适应 token 限制。",
          createdAt: now,
        },
      ];
    }

    // ── Budget Warning ─────────────────────────────────────────
    case "budget.warning": {
      const ev = event as unknown as {
        bucket?: string;
        percent?: number;
        will_compact?: boolean;
      };
      const pct = ev.percent ? `${Math.round(ev.percent * 100)}%` : "";
      return [
        {
          kind: "status_notice",
          id: nextId("budget"),
          tone: ev.will_compact ? "warning" : "info",
          title: ev.will_compact
            ? `上下文即将压缩 (${pct})`
            : `Token 用量 ${pct}`,
          message: ev.will_compact
            ? "接近 token 预算上限，即将自动压缩对话历史。"
            : undefined,
          createdAt: now,
        },
      ];
    }

    // ── Agent Errors ───────────────────────────────────────────
    case "error": {
      const ev = event as unknown as {
        message?: string;
        error_type?: string;
        recoverable?: boolean;
        error_code?: string;
        provider_error_type?: string;
      };
      const cells: (StatusNoticeCellState | ErrorCellState)[] = [];

      if (ev.error_code === "agent.busy") {
        cells.push({
          kind: "status_notice",
          id: nextId("busy"),
          tone: "warning",
          title: "Agent 正忙",
          message: "上一个任务仍在进行中，请等待完成后再发送新消息。",
          createdAt: now,
        });
        return cells;
      }

      if (ev.error_code === "workspace_missing") {
        cells.push({
          kind: "status_notice",
          id: nextId("workspace"),
          tone: "warning",
          title: "工作区未绑定",
          message: "请先打开一个工作区文件夹。",
          createdAt: now,
        });
        return cells;
      }

      cells.push({
        kind: "error",
        id: nextId("err"),
        title: ev.error_type === "blocked" ? "请求被阻止" : "处理出错",
        message: ev.message ?? "发生未知错误。",
        source:
          ev.error_type === "billing" ? "agent" : ev.provider_error_type ? "network" : "agent",
        recoverable: ev.recoverable ?? true,
        rawError: ev.message,
        createdAt: now,
      });

      return cells;
    }

    // ── Turn Completed (no cells needed, just a marker) ────────
    case "done":
      return [];

    default:
      return [];
  }
}
