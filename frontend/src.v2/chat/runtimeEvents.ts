import { useAppStore } from "../stores";
import type { ServerEvent, TaskUpdateEvent } from "../protocol/events";
import type { McpServerStatus } from "../stores/types";
import { sendClientCommand } from "../protocol/ws-outbox";
import { fromBackendPermissionMode } from "../protocol/permissions";
import { pushToast } from "../overlays/ToastContainer";

const subagentProgressSummary = (ev: {
  detail?: string;
  tool_name?: string;
  iteration?: number;
  max_iterations?: number;
}): string => {
  const detail = String(ev.detail ?? "").trim();
  if (detail) return detail;
  const toolName = String(ev.tool_name ?? "").trim();
  if (toolName) return `Running ${toolName}`;
  if (typeof ev.iteration === "number" && typeof ev.max_iterations === "number" && ev.max_iterations > 0) {
    return "Working through the task";
  }
  if (typeof ev.iteration === "number") return "Working through the task";
  return "Working";
};

const isActiveConversationEvent = (conversationId?: string): boolean => {
  if (!conversationId) return true;
  return useAppStore.getState().conversationId === conversationId;
};

const todoPatchFromEvent = (
  ev: TaskUpdateEvent & { todo_id?: string; status?: string; content?: string; activeForm?: string },
  activeFormFallback: string,
) => ({
  status: ev.status as "pending" | "in_progress" | "completed" | "blocked",
  content: ev.content ?? "",
  activeForm: ev.activeForm ?? activeFormFallback,
});

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
      // Don't show debug-visibility progress in chat timeline (e.g. iteration counters)
      const isDebug = ev.visibility === "debug";
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
        if (!isDebug) s.appendProgress(progress, conversationId);
        s.appendAgentProgress(progress, conversationId);
      }
      return true;
    }
    case "context_usage": {
      if (!isActiveConversationEvent(conversationId)) return true;
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
      if (!isActiveConversationEvent(conversationId)) return true;
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
    case "plan_updated": {
      const ev = e as unknown as {
        plan_id?: string;
        status?: string;
        current_step?: number;
        steps?: { id?: string; title?: string; status?: string; detail?: string }[];
      };
      if (!Array.isArray(ev.steps)) return true;
      const validStatus = new Set(["pending", "running", "done", "skipped", "failed"]);
      s.setPlan({
        planId: ev.plan_id || "plan",
        status: ev.status === "completed" || ev.status === "cancelled" || ev.status === "draft" || ev.status === "accepted"
          ? ev.status
          : "executing",
        currentStep: typeof ev.current_step === "number" ? ev.current_step : 0,
        steps: ev.steps.map((step, idx) => ({
          id: step.id || `step-${idx}`,
          title: step.title || `Step ${idx + 1}`,
          detail: step.detail,
          status: (step.status && validStatus.has(step.status) ? step.status : "pending") as
            "pending" | "running" | "done" | "skipped" | "failed",
        })),
      });
      // SidebarRight auto-switches to the Plan tab when status is "executing".
      return true;
    }
    case "plan_step_updated": {
      const ev = e as unknown as {
        plan_id?: string;
        step_id?: string;
        step_index?: number;
        status?: "pending" | "running" | "done" | "skipped" | "failed";
        title?: string;
        detail?: string;
        current_step?: number;
      };
      const current = useAppStore.getState().plan;
      if (!current || (ev.plan_id && current.planId !== ev.plan_id)) return true;
      const index = typeof ev.step_index === "number"
        ? ev.step_index
        : current.steps.findIndex((step) => step.id === ev.step_id);
      if (index < 0 || index >= current.steps.length || !ev.status) return true;
      const steps = current.steps.slice();
      steps[index] = {
        ...steps[index],
        status: ev.status,
        ...(ev.title ? { title: ev.title } : {}),
        ...(ev.detail ? { detail: ev.detail } : {}),
      };
      s.setPlan({
        ...current,
        steps,
        currentStep: ev.current_step ?? index,
        status: current.status === "completed" || current.status === "cancelled" ? current.status : "executing",
      });
      return true;
    }
    case "task.update": {
      const ev = e as TaskUpdateEvent;

      if ("session" in ev && ev.session) {
        s.setRuntimeSession(ev.session);
        if (ev.session.permission_mode) {
          useAppStore.setState({ permissionMode: fromBackendPermissionMode(ev.session.permission_mode) });
        }
        return true;
      }
      if (!("todo_id" in ev) || !ev.todo_id) return true;

      const existing = useAppStore.getState().todos.find((todo) => todo.id === ev.todo_id);
      if (existing) {
        const patch = todoPatchFromEvent(ev, existing.activeForm);
        if (
          existing.status !== patch.status ||
          existing.content !== patch.content ||
          existing.activeForm !== patch.activeForm
        ) {
          s.updateTodo(ev.todo_id, patch);
        }
      } else {
        const patch = todoPatchFromEvent(ev, "");
        useAppStore.setState({
          todos: [
            ...useAppStore.getState().todos,
            {
              id: ev.todo_id,
              ...patch,
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
    case "subagent.progress": {
      const ev = e as unknown as {
        subagent_id?: string;
        iteration?: number;
        max_iterations?: number;
        tool_name?: string;
        detail?: string;
      };
      const subagentId = String(ev.subagent_id || "").trim();
      if (!subagentId) return true;
      const existing = useAppStore.getState().subagents.find((subagent) => subagent.id === subagentId);
      const patch = {
        status: "running" as const,
        summary: subagentProgressSummary(ev),
        iteration: ev.iteration,
        maxIterations: ev.max_iterations,
        currentTool: ev.tool_name,
        detail: ev.detail,
      };
      if (existing) {
        s.updateSubagent(subagentId, patch);
      } else {
        s.addSubagent({ id: subagentId, role: "subagent", ...patch });
      }
      if (!useAppStore.getState().rightStackTabLocked) {
        s.setRightStackTab("subagents", { automatic: true });
      }
      return true;
    }
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
      if (!isActiveConversationEvent(conversationId)) return true;
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
      if (!isActiveConversationEvent(conversationId)) return true;
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
    case "mcp.lifecycle": {
      // Per-server lifecycle: update the matching connector compactly. Consumed
      // here (returns true) so it is never rendered as chat text.
      const ev = e as unknown as {
        server_name?: string;
        status?: string;
        phase?: McpServerStatus["phase"];
        message?: string;
        recoverable?: boolean;
        requires_user_action?: boolean;
        setup_hint?: string;
        docs_url?: string;
      };
      const name = String(ev.server_name ?? "").trim();
      if (!name) return true;
      const servers = useAppStore.getState().mcpServers;
      const idx = servers.findIndex((srv) => srv.name === name);
      const errorish = ev.phase === "failed" || ev.phase === "auth_required" || ev.phase === "expired";
      const patch = {
        ...(ev.status ? { status: ev.status as McpServerStatus["status"] } : {}),
        phase: ev.phase,
        recoverable: ev.recoverable,
        requiresUserAction: ev.requires_user_action,
        setupHint: ev.setup_hint,
        docsUrl: ev.docs_url,
        lastError: errorish ? ev.message : undefined,
      };
      if (idx >= 0) {
        const next = servers.slice();
        next[idx] = { ...next[idx], ...patch };
        s.setMcpServers(next);
      } else {
        s.setMcpServers([...servers, { name, status: "offline", ...patch }]);
      }
      return true;
    }
    case "mcp.progress": {
      // Coarse connect/reconnect progress; stored on the server, not in chat.
      const ev = e as unknown as {
        server_name?: string;
        operation?: string;
        message?: string;
        progress?: number;
        status?: "running" | "completed" | "failed";
      };
      const name = String(ev.server_name ?? "").trim();
      if (!name) return true;
      const servers = useAppStore.getState().mcpServers;
      const idx = servers.findIndex((srv) => srv.name === name);
      if (idx < 0) return true;
      const next = servers.slice();
      next[idx] = {
        ...next[idx],
        progress: {
          operation: String(ev.operation ?? "operation"),
          message: ev.message,
          progress: typeof ev.progress === "number" ? ev.progress : undefined,
          status: ev.status ?? "running",
        },
      };
      s.setMcpServers(next);
      return true;
    }
    default:
      return false;
  }
};
