import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { pushToast } from "../overlays/ToastContainer";

export const handleRuntimeEvent = (e: ServerEvent, conversationId?: string): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "agent.progress": {
      const ev = e as unknown as {
        id?: string;
        stage?: "status" | "planning" | "tool" | "approval" | "verification" | "final";
        phase?: "orienting" | "planning" | "model" | "tool" | "approval" | "verify" | "final" | "recover" | "status";
        status?: "running" | "completed" | "failed" | "info";
        message?: string;
        label?: string;
        summary?: string;
        visibility?: "timeline" | "compact" | "debug";
        detail?: string;
        tool_call_id?: string;
        tool_name?: string;
        group_id?: string;
        step_id?: string;
        count?: number;
      };
      const message = String(ev.message ?? "").trim();
      if (message) {
        const progress = {
          id: String(ev.id || `${ev.stage || "status"}:${message}`),
          stage: ev.stage || "status",
          phase: ev.phase,
          status: ev.status || "info",
          message,
          label: ev.label,
          summary: ev.summary,
          visibility: ev.visibility,
          detail: ev.detail,
          toolCallId: ev.tool_call_id,
          toolName: ev.tool_name,
          groupId: ev.group_id,
          stepId: ev.step_id,
          count: ev.count,
        };
        s.appendProgress(progress, conversationId);
        s.appendAgentProgress(progress, conversationId);
      }
      return true;
    }
    case "context_usage": {
      const ev = e as unknown as { used?: number; limit?: number };
      if (ev.used != null && ev.limit != null) {
        const currentUsage = useAppStore.getState().contextUsage;
        s.setContextUsage({
          used: ev.used,
          limit: ev.limit,
          compactedAt: currentUsage?.compactedAt,
          compactSummary: currentUsage?.compactSummary,
        });
      }
      return true;
    }
    case "context_compacted": {
      const summary = (e as unknown as { summary?: string }).summary ?? "Context compacted.";
      const currentUsage = useAppStore.getState().contextUsage;
      s.setContextUsage({
        used: currentUsage?.used ?? 0,
        limit: currentUsage?.limit ?? 0,
        compactedAt: Date.now(),
        compactSummary: summary,
      });
      sendClientCommand({ type: "session.usage.inspect" });
      s.upsertSystemMessage(
        "system-compact-status",
        "Context compacted. Summary saved to session memory.",
        { conversationId, replacePrefix: "Compacting context" },
      );
      return true;
    }
    case "plan.update":
      s.setPlan({
        planId: e.plan_id,
        status: e.status,
        steps: e.steps,
        currentStep: e.current_step ?? 0,
      });
      return true;
    case "task.update": {
      const existing = useAppStore.getState().todos.find((todo) => todo.id === e.todo_id);
      if (existing) {
        s.updateTodo(e.todo_id, {
          status: e.status,
          content: e.content,
          activeForm: e.activeForm ?? existing.activeForm,
        });
      } else {
        useAppStore.setState({
          todos: [
            ...useAppStore.getState().todos,
            {
              id: e.todo_id,
              status: e.status,
              content: e.content,
              activeForm: e.activeForm ?? "",
            },
          ],
        });
      }
      return true;
    }
    case "subagent.start":
      s.addSubagent({ id: e.subagent_id, role: e.role, status: "running" });
      if (!useAppStore.getState().rightStackTabLocked) {
        s.setRightStackTab("subagents", { automatic: true });
      }
      return true;
    case "subagent.done": {
      const existing = useAppStore.getState().subagents.find((subagent) => subagent.id === e.subagent_id);
      if (existing) {
        s.updateSubagent(e.subagent_id, {
          status: e.error ? "error" : "done",
          summary: e.error || e.summary || existing.summary,
        });
      } else {
        s.addSubagent({
          id: e.subagent_id,
          role: "subagent",
          status: e.error ? "error" : "done",
          summary: e.error || e.summary || "",
        });
      }
      return true;
    }
    case "budget_update": {
      const used = e.used as number | undefined;
      const total = e.total as number | undefined;
      const breakdown = e.breakdown as Record<string, number> | undefined;
      if (used != null && total != null) {
        const currentUsage = useAppStore.getState().contextUsage;
        const buckets = breakdown
          ? Object.entries(breakdown).map(([name, tokens]) => ({ name, used: tokens, limit: 0 }))
          : [];
        const percent = total > 0 ? used / total : 0;
        s.setBudget(buckets, percent);
        s.setContextUsage({
          used,
          limit: total,
          compactedAt: currentUsage?.compactedAt,
          compactSummary: currentUsage?.compactSummary,
        });
      }
      return true;
    }
    case "budget.warning": {
      const current = useAppStore.getState();
      s.setBudget(current.budgetBuckets, e.percent);
      if (e.percent >= 0.9) {
        pushToast(`Token budget at ${Math.round(e.percent * 100)}%`, "warning");
      }
      return true;
    }
    case "permission.mode.updated": {
      const ev = e as unknown as { mode?: string };
      if (ev.mode) {
        useAppStore.setState({ permissionMode: fromBackendPermissionMode(ev.mode) });
      }
      return true;
    }
    default:
      return false;
  }
};
