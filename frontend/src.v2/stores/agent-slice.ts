import type { StateCreator } from "zustand";
import type { AppStore, AgentSlice } from "./types";
import {
  progressConversationKey,
  automaticRightPanelState,
} from "./shared-helpers";

export const createAgentSlice: StateCreator<AppStore, [], [], AgentSlice> = (set, get) => ({
  plan: null,
  todos: [],
  subagents: [],
  agentProgress: [],
  runtimeSession: null,
  budgetBuckets: [],
  totalBudgetPercent: 0,
  setPlan: (p) =>
    set((s) => ({
      plan: p,
      ...(p && p.status !== "completed" && p.status !== "cancelled"
        ? automaticRightPanelState(s, "plan")
        : {}),
    })),
  updatePlanStep: (idx, status) =>
    set((s) => {
      if (!s.plan) return s;
      const steps = s.plan.steps.slice();
      if (idx < 0 || idx >= steps.length) return s;
      steps[idx] = { ...steps[idx], status };
      return { plan: { ...s.plan, steps } };
    }),
  setTodos: (t) => set({ todos: t }),
  addTodo: (todo) =>
    set((s) => ({
      todos: [...s.todos, todo],
    })),
  removeTodo: (id) =>
    set((s) => ({
      todos: s.todos.filter((todo) => todo.id !== id),
    })),
  updateTodo: (id, patch) =>
    set((s) => {
      const index = s.todos.findIndex((todo) => todo.id === id);
      if (index < 0) return s;
      const current = s.todos[index];
      const nextTodo = { ...current, ...patch };
      if (
        current.status === nextTodo.status &&
        current.content === nextTodo.content &&
        current.activeForm === nextTodo.activeForm
      ) {
        return s;
      }
      const todos = s.todos.slice();
      todos[index] = nextTodo;
      const hasRunningTask = todos.some((todo) => todo.status === "in_progress");
      return {
        todos,
        ...(hasRunningTask ? automaticRightPanelState(s, "tasks") : {}),
      };
    }),
  addSubagent: (sa) =>
    set((s) => ({
      subagents: [
        ...s.subagents.filter((existing) => existing.id !== sa.id),
        sa,
      ].slice(-20),
    })),
  updateSubagent: (id, patch) =>
    set((s) => ({
      subagents: s.subagents.map((sa) => (sa.id === id ? { ...sa, ...patch } : sa)),
    })),
  removeSubagent: (id) =>
    set((s) => ({ subagents: s.subagents.filter((sa) => sa.id !== id) })),
  setRuntimeSession: (session) => set({ runtimeSession: session }),
  appendAgentProgress: (progress, conversationId) =>
    set((s) => {
      const timestamp = Date.now();
      const key = progressConversationKey(conversationId || s.conversationId || undefined);
      const entry = {
        ...progress,
        type: "progress" as const,
        timestamp,
        conversationId: key,
      };
      const existingIdx = s.agentProgress.findIndex((item) =>
        item.conversationId === key && item.id === progress.id,
      );
      // Don't auto-pop right panel for tool-level progress.
      // Only auto-open for plan-related phases.
      const rightPanelPatch = entry.status === "running" && entry.stage === "planning"
        ? automaticRightPanelState(s, "plan")
        : {};
      if (existingIdx >= 0) {
        const next = s.agentProgress.slice();
        next[existingIdx] = entry;
        return { agentProgress: next.slice(-80), ...rightPanelPatch };
      }
      return { agentProgress: [...s.agentProgress, entry].slice(-80), ...rightPanelPatch };
    }),
  finishAgentProgress: (conversationId, status = "completed") =>
    set((s) => {
      const key = progressConversationKey(conversationId || s.conversationId || undefined);
      const timestamp = Date.now();
      return {
        agentProgress: s.agentProgress.map((item) =>
          item.conversationId === key && item.status === "running"
            ? { ...item, status, timestamp }
            : item,
        ),
      };
    }),
  clearAgentProgress: (conversationId) =>
    set((s) => {
      const key = progressConversationKey(conversationId || s.conversationId || undefined);
      return {
        agentProgress: s.agentProgress.filter((item) => item.conversationId !== key),
      };
    }),
  setBudget: (buckets, total) =>
    set({ budgetBuckets: buckets, totalBudgetPercent: total }),
});
