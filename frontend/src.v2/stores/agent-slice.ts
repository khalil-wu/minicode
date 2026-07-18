import type { StateCreator } from "zustand";
import type { AgentSlice } from "./types";
import { progressConversationKey } from "./shared-helpers";
import type { AgentProgressEntry, AppStore, ConversationAgentState, ProgressContentBlock } from "./types";
import { capabilityFeatureEnabled } from "../protocol/capabilities";

const SERIAL_MAIN_PROGRESS_STAGES = new Set<ProgressContentBlock["stage"]>([
  "planning",
  "verification",
  "final",
  "status",
]);

function shouldSerializeMainAgentProgress(progress: Omit<ProgressContentBlock, "type" | "timestamp">): boolean {
  return (
    progress.status === "running" &&
    SERIAL_MAIN_PROGRESS_STAGES.has(progress.stage) &&
    progress.stage !== "tool" &&
    progress.stage !== "approval"
  );
}

function completePreviousMainAgentProgress(
  items: AgentProgressEntry[],
  key: string,
  next: Omit<ProgressContentBlock, "type" | "timestamp">,
  timestamp: number,
): AgentProgressEntry[] {
  if (!shouldSerializeMainAgentProgress(next)) return items;
  return items.map((item) => {
    if (item.conversationId !== key) return item;
    if (item.id === next.id) return item;
    if (item.status !== "running") return item;
    if (!SERIAL_MAIN_PROGRESS_STAGES.has(item.stage)) return item;
    if (item.stage === "tool" || item.stage === "approval") return item;
    return { ...item, status: "completed", timestamp };
  });
}

function emptyConversationAgentState(): ConversationAgentState {
  return {
    plan: null,
    todos: [],
    subagents: [],
    agentProgress: [],
  };
}

function cloneConversationAgentState(state: ConversationAgentState): ConversationAgentState {
  return {
    plan: state.plan,
    todos: state.todos.slice(),
    subagents: state.subagents.slice(),
    agentProgress: state.agentProgress.slice(),
  };
}

function liveConversationAgentState(s: AppStore): ConversationAgentState {
  return {
    plan: s.plan,
    todos: s.todos.slice(),
    subagents: s.subagents.slice(),
    agentProgress: s.agentProgress.slice(),
  };
}

function upsertSubagentStable(
  subagents: ConversationAgentState["subagents"],
  nextSubagent: ConversationAgentState["subagents"][number],
): ConversationAgentState["subagents"] {
  const index = subagents.findIndex((existing) => existing.id === nextSubagent.id);
  if (index >= 0) {
    const next = subagents.slice();
    next[index] = { ...next[index], ...nextSubagent };
    return next;
  }
  return [...subagents, nextSubagent];
}

function targetConversationId(s: AppStore, conversationId?: string): string | undefined {
  return conversationId || s.conversationId || undefined;
}

function isActiveConversationTarget(s: AppStore, conversationId?: string): boolean {
  const targetId = targetConversationId(s, conversationId);
  return !targetId || targetId === s.conversationId;
}

function getStoredConversationAgentState(s: AppStore, conversationId: string): ConversationAgentState {
  if (conversationId === s.conversationId) return liveConversationAgentState(s);
  return cloneConversationAgentState(s.conversationAgentStates?.[conversationId] ?? emptyConversationAgentState());
}

function storeConversationAgentState(
  s: AppStore,
  conversationId: string,
  state: ConversationAgentState,
): Record<string, ConversationAgentState> {
  return {
    ...(s.conversationAgentStates ?? {}),
    [conversationId]: cloneConversationAgentState(state),
  };
}

export const createAgentSlice: StateCreator<AppStore, [], [], AgentSlice> = (set, get) => ({
  plan: null,
  todos: [],
  subagents: [],
  agentProgress: [],
  conversationAgentStates: {},
  runtimeSession: null,
  runtimeCapabilities: null,
  budgetBuckets: [],
  totalBudgetPercent: 0,
  setPlan: (p, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      if (!active && targetId) {
        const targetState = getStoredConversationAgentState(s, targetId);
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            plan: p,
          }),
        };
      }
      const patch = {
        plan: p,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...liveConversationAgentState(s),
                plan: p,
              }),
            }
          : {}),
      };
      return patch;
    }),
  updatePlanStep: (idx, status) =>
    set((s) => {
      if (!s.plan) return s;
      const steps = s.plan.steps.slice();
      if (idx < 0 || idx >= steps.length) return s;
      steps[idx] = { ...steps[idx], status };
      const plan = { ...s.plan, steps };
      return {
        plan,
        ...(s.conversationId
          ? {
              conversationAgentStates: storeConversationAgentState(s, s.conversationId, {
                ...liveConversationAgentState(s),
                plan,
              }),
            }
          : {}),
      };
    }),
  setTodos: (t, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      if (!active && targetId) {
        const targetState = getStoredConversationAgentState(s, targetId);
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            todos: t.slice(),
          }),
        };
      }
      return {
        todos: t,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...liveConversationAgentState(s),
                todos: t.slice(),
              }),
            }
          : {}),
      };
    }),
  addTodo: (todo, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const todos = [...targetState.todos, todo];
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            todos,
          }),
        };
      }
      return {
        todos,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                todos,
              }),
            }
          : {}),
      };
    }),
  removeTodo: (id, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const todos = targetState.todos.filter((todo) => todo.id !== id);
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            todos,
          }),
        };
      }
      return {
        todos,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                todos,
              }),
            }
          : {}),
      };
    }),
  updateTodo: (id, patch, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const index = targetState.todos.findIndex((todo) => todo.id === id);
      if (index < 0) return s;
      const current = targetState.todos[index];
      const nextTodo = { ...current, ...patch };
      if (
        current.status === nextTodo.status &&
        current.content === nextTodo.content &&
        current.activeForm === nextTodo.activeForm
      ) {
        return s;
      }
      const todos = targetState.todos.slice();
      todos[index] = nextTodo;
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            todos,
          }),
        };
      }
      return {
        todos,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                todos,
              }),
            }
          : {}),
      };
    }),
  addSubagent: (sa, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const subagents = upsertSubagentStable(targetState.subagents, sa);
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            subagents,
          }),
        };
      }
      return {
        subagents,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                subagents,
              }),
            }
          : {}),
      };
    }),
  updateSubagent: (id, patch, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const existing = targetState.subagents.find((sa) => sa.id === id);
      const terminalStatuses = new Set(["done", "partial", "cancelled", "error"]);
      const incomingStatus = patch.status;
      const safePatch = existing
        && terminalStatuses.has(existing.status)
        && incomingStatus
        && !terminalStatuses.has(incomingStatus)
        ? { ...patch, status: existing.status }
        : patch;
      const subagents = existing
        ? targetState.subagents.map((sa) => (sa.id === id ? { ...sa, ...safePatch } : sa))
        : upsertSubagentStable(targetState.subagents, {
            id,
            role: patch.role || "subagent",
            status: patch.status || "pending",
            ...patch,
          });
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            subagents,
          }),
        };
      }
      return {
        subagents,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                subagents,
              }),
            }
          : {}),
      };
    }),
  removeSubagent: (id, conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const subagents = targetState.subagents.filter((sa) => sa.id !== id);
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            subagents,
          }),
        };
      }
      return {
        subagents,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                subagents,
              }),
            }
          : {}),
      };
    }),
  setRuntimeSession: (session) => set({ runtimeSession: session }),
  setRuntimeCapabilities: (capabilities) =>
    set((s) => ({
      runtimeCapabilities: capabilities,
      ...(!capabilityFeatureEnabled(capabilities, "global_search", true)
        ? { quickOpenVisible: false, quickOpenResults: [], quickOpenLoading: false }
        : {}),
      ...(!capabilityFeatureEnabled(capabilities, "agent_editor", true) && s.agentEditorOpen
        ? { agentEditorOpen: false }
        : {}),
    })),
  appendAgentProgress: (progress, conversationId) =>
    set((s) => {
      const timestamp = Date.now();
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const key = progressConversationKey(targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const entry = {
        ...progress,
        type: "progress" as const,
        timestamp,
        conversationId: key,
      };
      const existingIdx = targetState.agentProgress.findIndex((item) =>
        item.conversationId === key && item.id === progress.id,
      );
      const serializedProgress = completePreviousMainAgentProgress(targetState.agentProgress, key, progress, timestamp);
      let agentProgress: AgentProgressEntry[];
      if (existingIdx >= 0) {
        const next = serializedProgress.slice();
        next[existingIdx] = entry;
        agentProgress = next.slice(-80);
      } else {
        agentProgress = [...serializedProgress, entry].slice(-80);
      }
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            agentProgress,
          }),
        };
      }
      return {
        agentProgress,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                agentProgress,
              }),
            }
          : {}),
      };
    }),
  finishAgentProgress: (conversationId, status = "completed") =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const key = progressConversationKey(targetId);
      const timestamp = Date.now();
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const agentProgress = targetState.agentProgress.map((item) =>
        item.conversationId === key && item.status === "running"
          ? { ...item, status, timestamp }
          : item,
      );
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            agentProgress,
          }),
        };
      }
      return {
        agentProgress,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                agentProgress,
              }),
            }
          : {}),
      };
    }),
  clearAgentProgress: (conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      const active = isActiveConversationTarget(s, targetId);
      const key = progressConversationKey(targetId);
      const targetState = active ? liveConversationAgentState(s) : targetId ? getStoredConversationAgentState(s, targetId) : liveConversationAgentState(s);
      const agentProgress = targetState.agentProgress.filter((item) => item.conversationId !== key);
      if (!active && targetId) {
        return {
          conversationAgentStates: storeConversationAgentState(s, targetId, {
            ...targetState,
            agentProgress,
          }),
        };
      }
      return {
        agentProgress,
        ...(targetId
          ? {
              conversationAgentStates: storeConversationAgentState(s, targetId, {
                ...targetState,
                agentProgress,
              }),
            }
          : {}),
      };
    }),
  snapshotAgentState: (conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      if (!targetId) return s;
      return {
        conversationAgentStates: storeConversationAgentState(s, targetId, liveConversationAgentState(s)),
      };
    }),
  restoreAgentState: (conversationId) =>
    set((s) => {
      const targetId = targetConversationId(s, conversationId);
      if (!targetId) {
        return {
          plan: null,
          todos: [],
          subagents: [],
          agentProgress: [],
        };
      }
      const stored = cloneConversationAgentState(s.conversationAgentStates?.[targetId] ?? emptyConversationAgentState());
      return {
        ...stored,
        conversationAgentStates: storeConversationAgentState(s, targetId, stored),
      };
    }),
  clearConversationAgentState: (conversationId) =>
    set((s) => {
      const next = { ...(s.conversationAgentStates ?? {}) };
      delete next[conversationId];
      if (s.conversationId !== conversationId) return { conversationAgentStates: next };
      return {
        plan: null,
        todos: [],
        subagents: [],
        agentProgress: [],
        conversationAgentStates: next,
      };
    }),
  setBudget: (buckets, total) =>
    set({ budgetBuckets: buckets, totalBudgetPercent: total }),
});
