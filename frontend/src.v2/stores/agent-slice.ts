import type { StateCreator } from "zustand";
import type { AgentSlice } from "./types";
import { progressConversationKey } from "./shared-helpers";
import type { AgentProgressEntry, AppStore, ConversationAgentState, ProgressContentBlock } from "./types";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { providerProgressLifecycleRegressed } from "../lib/provider-progress";

const SERIAL_MAIN_PROGRESS_STAGES = new Set<ProgressContentBlock["stage"]>([
  "planning",
  "final",
  "status",
]);

const PROVIDER_PROGRESS_TERMINAL_STATUSES = new Set(["partial", "completed", "failed"]);

function mergeProgressDetail(previous?: string, incoming?: string): string | undefined {
  const parts: string[] = [];
  const seen = new Set<string>();
  for (const detail of [previous, incoming]) {
    for (const rawPart of String(detail ?? "").split(" · ")) {
      const part = rawPart.trim();
      if (!part || seen.has(part)) continue;
      seen.add(part);
      parts.push(part);
    }
  }
  return parts.length > 0 ? parts.join(" · ") : undefined;
}

const maxProgressNumber = (left: number | undefined, right: number | undefined): number | undefined => {
  if (left === undefined) return right;
  if (right === undefined) return left;
  return Math.max(left, right);
};

const isProviderProgressId = (id: string): boolean => id.startsWith("provider:");

const isProviderRetryProgress = (
  progress: Pick<ProgressContentBlock, "id" | "retryAttempt" | "maxRetries" | "providerState">,
  previous?: Pick<ProgressContentBlock, "retryAttempt" | "maxRetries" | "providerState">,
): boolean => isProviderProgressId(progress.id) && (
  typeof progress.retryAttempt === "number"
  || typeof progress.maxRetries === "number"
  || Boolean(progress.providerState)
  || typeof previous?.retryAttempt === "number"
  || typeof previous?.maxRetries === "number"
  || Boolean(previous?.providerState)
);

function mergeProgressEntry(
  previous: AgentProgressEntry | undefined,
  progress: Omit<ProgressContentBlock, "type" | "timestamp">,
  timestamp: number,
  conversationId: string,
): AgentProgressEntry {
  const incoming = Object.fromEntries(
    Object.entries(progress).filter(([, value]) => value !== undefined),
  ) as Omit<ProgressContentBlock, "type" | "timestamp">;
  const isProviderProgress = isProviderProgressId(progress.id);
  const isRetryProgress = isProviderRetryProgress(progress, previous);
  if (!previous || !isProviderProgress) {
    const entry: AgentProgressEntry = {
      ...previous,
      ...incoming,
      type: "progress",
      timestamp,
      conversationId,
    };
    const detail = mergeProgressDetail(previous?.detail, progress.detail);
    if (detail) entry.detail = detail;
    if (entry.status !== "running") delete entry.ephemeral;
    return entry;
  }

  const lifecycleRegressed = providerProgressLifecycleRegressed(previous, progress);

  // A provider row is a single logical retry ladder. Once it reaches a
  // stronger lifecycle state, a delayed frame from an older attempt must not
  // reopen it or replace its terminal message. Higher retry counters remain
  // visible even when the frame carrying them arrived out of order.
  const entry: AgentProgressEntry = lifecycleRegressed
    ? { ...previous, type: "progress", timestamp: previous.timestamp, conversationId }
    : {
        ...previous,
        ...incoming,
        type: "progress",
        // A provider retry is one logical timeline row. Keep its first-seen
        // timestamp as the row's start even as later attempts update status
        // and counters; otherwise the row jumps forward on every retry.
        timestamp: previous.timestamp,
        conversationId,
      };

  // Once a terminal provider state is visible, a delayed non-terminal frame
  // is a fenced diagnostic. Do not let its counters rewrite the terminal row.
  const terminalFence = lifecycleRegressed
    && PROVIDER_PROGRESS_TERMINAL_STATUSES.has(previous.status);
  if (!terminalFence) {
    const retryAttempt = maxProgressNumber(previous.retryAttempt, progress.retryAttempt);
    const maxRetries = maxProgressNumber(previous.maxRetries, progress.maxRetries);
    const count = maxProgressNumber(previous.count, progress.count);
    if (retryAttempt !== undefined) entry.retryAttempt = retryAttempt;
    if (maxRetries !== undefined) entry.maxRetries = maxRetries;
    if (count !== undefined) entry.count = count;
  }

  if (!lifecycleRegressed) {
    const detail = String(progress.detail ?? "").trim();
    if (isRetryProgress) {
      if (detail) entry.detail = detail;
      else delete entry.detail;
    } else {
      const mergedDetail = mergeProgressDetail(previous.detail, progress.detail);
      if (mergedDetail) entry.detail = mergedDetail;
    }
  }

  if (entry.status !== "running") delete entry.ephemeral;
  return entry;
}

function shouldSerializeMainAgentProgress(progress: Omit<ProgressContentBlock, "type" | "timestamp">): boolean {
  return (
    !isProviderProgressId(progress.id) &&
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
    if (isProviderProgressId(item.id)) return item;
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
  turnDiffs: {},
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
  setTurnDiff: (conversationId, diff) =>
    set((s) => {
      const owner = conversationId.trim();
      if (!owner) return s;
      const next = { ...s.turnDiffs };
      if (diff) next[owner] = diff;
      else delete next[owner];
      return { turnDiffs: next };
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
      const existingIdx = targetState.agentProgress.findIndex((item) =>
        item.conversationId === key && item.id === progress.id,
      );
      const serializedProgress = completePreviousMainAgentProgress(targetState.agentProgress, key, progress, timestamp);
      const previous = existingIdx >= 0 ? serializedProgress[existingIdx] : undefined;
      const entry = mergeProgressEntry(previous, progress, timestamp, key);
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
      const nextTurnDiffs = { ...s.turnDiffs };
      delete next[conversationId];
      delete nextTurnDiffs[conversationId];
      if (s.conversationId !== conversationId) return { conversationAgentStates: next, turnDiffs: nextTurnDiffs };
      return {
        plan: null,
        todos: [],
        subagents: [],
        agentProgress: [],
        conversationAgentStates: next,
        turnDiffs: nextTurnDiffs,
      };
    }),
  setBudget: (buckets, total) =>
    set({ budgetBuckets: buckets, totalBudgetPercent: total }),
});
