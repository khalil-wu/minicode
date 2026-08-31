import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleNoticeEvent } from "./noticeEvents";
import { handleRuntimeEvent as handleRuntimeEventImpl } from "./runtimeEvents";
import { handleChatStreamEvent } from "./chatStreamEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

// Runtime events are owner-scoped on the wire. Keep fixture literals readable
// while supplying the same explicit owner the backend sends.
const handleRuntimeEvent = (event: ServerEvent, owner?: string): boolean => {
  const activeOwner = owner || useAppStore.getState().conversationId || "";
  const payload = event as unknown as { type?: string; conversation_id?: unknown; session?: unknown };
  const turnScoped = new Set([
    "agent.progress", "runtime.span", "agent.run.started", "agent.run.completed",
    "turn.plan.updated", "task.update", "subagent.start",
    "subagent.event", "subagent.progress", "subagent.done",
  ]);
  if (!payload.conversation_id && activeOwner && turnScoped.has(String(payload.type)) && !payload.session) {
    return handleRuntimeEventImpl({ ...payload, conversation_id: activeOwner } as unknown as ServerEvent, owner);
  }
  return handleRuntimeEventImpl(event, owner);
};

describe("runtime compaction events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-runtime",
      messages: [],
      conversationMessages: {},
      contextUsage: {
        used: 120,
        limit: 1000,
      },
      inspectorEntries: [],
    });
  });

  it("combines conversation compaction state and runtime compacted notice without duplicate transcript entries", () => {
    expect(handleNoticeEvent({
      type: "conversation.compaction.updated",
      conversation_id: "conv-runtime",
      state: "compacted",
      summary: "Kept project goals and current task.",
      timestamp: "2026-08-15T10:00:00Z",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(handleRuntimeEvent({
      type: "context_compacted",
      conversation_id: "conv-runtime",
      summary: "Kept project goals and current task.",
      timestamp: "2026-08-15T10:00:00Z",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.contextUsage).toMatchObject({
      used: 120,
      limit: 1000,
      compactSummary: "Kept project goals and current task.",
      compactedAt: Date.parse("2026-08-15T10:00:00Z"),
    });
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      role: "system",
      content: "上下文已压缩，摘要已保存到会话记忆中。",
    });
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "session.usage.inspect" });
  });

  it("restores replayed compaction metadata without issuing a fresh usage request", () => {
    expect(handleRuntimeEvent({
      type: "context_compacted",
      conversation_id: "conv-runtime",
      summary: "Historical compacted context.",
      timestamp: "2026-08-14T04:05:06Z",
      replayed: true,
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(useAppStore.getState().contextUsage).toMatchObject({
      compactSummary: "Historical compacted context.",
      compactedAt: Date.parse("2026-08-14T04:05:06Z"),
    });
    expect(sendClientCommand).not.toHaveBeenCalled();
  });

  it("uses authoritative post-compaction tokens and keeps boundary evidence in Inspector", () => {
    expect(handleRuntimeEvent({
      type: "context_compacted",
      conversation_id: "conv-runtime",
      summary: "Retained the release goal and latest verification evidence.",
      before_tokens: 900,
      after_tokens: 240,
      retained_categories: ["system_runtime", "history"],
      ledger: {
        schema_version: 1,
        estimated_tokens: 240,
        actual_tokens: 0,
        compaction_count: 1,
        native_attachment_tokens: 0,
        native_attachment_count: 0,
        entries: [],
      },
      timestamp: "2026-08-15T10:00:00Z",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.contextUsage).toMatchObject({ used: 240, limit: 1000 });
    expect(state.messages[0]?.content).toContain("从 900 降至 240 tokens，节省 660");
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "budget",
        targetId: "compaction:conv-runtime",
        payload: expect.objectContaining({
          before_tokens: 900,
          after_tokens: 240,
          saved_tokens: 660,
          retained_categories: ["system_runtime", "history"],
        }),
      }),
    ]);
  });
});

describe("runtime context fork, ledger, and side-query projections", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [],
      conversationMessages: {},
      contextUsage: { used: 10, limit: 100 },
      inspectorEntries: [],
    });
  });

  it("projects branch activation into its real owner and preserves all fork facts", () => {
    expect(handleRuntimeEvent({
      type: "context_forked",
      conversation_id: "conv-branch",
      fork_id: "fork-activated",
      message_index: 4,
      context_history_index: 3,
      history_length: 8,
      estimated_tokens: 2048,
      parent_conversation_id: "conv-source",
      branch_conversation_id: "conv-branch",
      branch_created: true,
      branch_activated: true,
    } as unknown as ServerEvent, "conv-branch")).toBe(true);

    expect(handleRuntimeEvent({
      type: "context_forked",
      conversation_id: "conv-source",
      fork_id: "fork-unactivated",
      message_index: 2,
      context_history_index: 1,
      history_length: 5,
      estimated_tokens: 900,
      parent_conversation_id: "conv-source",
      branch_conversation_id: "conv-draft",
      branch_created: true,
      branch_activated: false,
    } as unknown as ServerEvent, "conv-source")).toBe(true);

    expect(useAppStore.getState().messages).toEqual([]);
    expect(useAppStore.getState().conversationMessages["conv-branch"]).toEqual([
      expect.objectContaining({
        id: "context-forked:fork-activated",
        role: "system",
        content: expect.stringContaining("已创建并切换到上下文分支（conv-branch）"),
      }),
    ]);
    expect(useAppStore.getState().conversationMessages["conv-branch"]?.[0]?.content).toContain(
      "从第 5 条可见消息分叉，保留 8 条模型历史，估算 2,048 tokens",
    );
    expect(useAppStore.getState().conversationMessages["conv-source"]).toEqual([
      expect.objectContaining({
        id: "context-forked:fork-unactivated",
        content: expect.stringContaining("已创建上下文分支（conv-draft）"),
      }),
    ]);
  });

  it("stores background side-query detail once per stable transport event", () => {
    const event = {
      type: "context_side_query_result",
      conversation_id: "conv-background",
      query: "Which release check is still missing?",
      focus: "browser verification",
      result: "Run the authenticated reconnect workflow.",
      event_id: "session-1:42",
      seq: 42,
    } as unknown as ServerEvent;

    expect(handleRuntimeEvent(event, "conv-background")).toBe(true);
    expect(handleRuntimeEvent({ ...event, replayed: true } as ServerEvent, "conv-background")).toBe(true);

    const messages = useAppStore.getState().conversationMessages["conv-background"] ?? [];
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({ id: "context-side-query:session-1:42", role: "system" });
    expect(messages[0]?.content).toContain("上下文旁路查询（聚焦：browser verification）");
    expect(messages[0]?.content).toContain("问题：Which release check is still missing?");
    expect(messages[0]?.content).toContain("结果：Run the authenticated reconnect workflow.");
  });

  it("updates the context ledger only for the active owner", () => {
    const ledger = {
      schema_version: 1,
      estimated_tokens: 1200,
      actual_tokens: 1250,
      compaction_count: 1,
      native_attachment_tokens: 200,
      native_attachment_count: 1,
      entries: [{
        category: "history",
        label: "Conversation history",
        estimated_tokens: 1000,
        item_count: 6,
        source_count: 1,
        sources: ["conversation"],
      }],
    };

    expect(handleRuntimeEvent({
      type: "context_ledger",
      conversation_id: "conv-background",
      ...ledger,
    } as unknown as ServerEvent, "conv-background")).toBe(true);
    expect(useAppStore.getState().contextUsage).toEqual({ used: 10, limit: 100 });
    expect(useAppStore.getState().inspectorEntries).toEqual([]);

    expect(handleRuntimeEvent({
      type: "context_ledger",
      conversation_id: "conv-active",
      ...ledger,
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(useAppStore.getState().contextUsage).toMatchObject({
      used: 10,
      limit: 100,
      ledger: expect.objectContaining({
        actual_tokens: 1250,
        native_attachment_count: 1,
      }),
    });
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "budget",
        targetId: "ledger:conv-active",
        payload: expect.objectContaining({ event: "context_ledger" }),
      }),
    ]);
  });
});

describe("runtime token budget events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-active",
      contextUsage: { used: 10, limit: 100 },
      budgetBuckets: [{ name: "prompt", used: 10, limit: 0 }],
      totalBudgetPercent: 0.1,
      inspectorEntries: [],
    });
  });

  it("ignores context usage updates for inactive conversations", () => {
    expect(handleRuntimeEvent({
      type: "context_usage",
      conversation_id: "conv-other",
      used: 90,
      limit: 100,
    } as unknown as ServerEvent, "conv-other")).toBe(true);

    expect(useAppStore.getState().contextUsage).toEqual({ used: 10, limit: 100 });
  });

  it("keeps inactive budget updates out of the active ring while retaining diagnostics", () => {
    expect(handleRuntimeEvent({
      type: "budget_update",
      conversation_id: "conv-other",
      used: 90,
      total: 100,
      breakdown: { prompt: 90 },
    } as unknown as ServerEvent, "conv-other")).toBe(true);

    const state = useAppStore.getState();
    expect(state.contextUsage).toEqual({ used: 10, limit: 100 });
    expect(state.budgetBuckets).toEqual([{ name: "prompt", used: 10, limit: 0 }]);
    expect(state.totalBudgetPercent).toBe(0.1);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "budget",
        targetId: "budget:conv-other",
        payload: expect.objectContaining({
          used: 90,
          total: 100,
          breakdown: { prompt: 90 },
        }),
      }),
    ]);
  });

  it("applies usage updates for the active conversation", () => {
    expect(handleRuntimeEvent({
      type: "budget_update",
      conversation_id: "conv-active",
      used: 42,
      total: 100,
      breakdown: { prompt: 30, tools: 12 },
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    const state = useAppStore.getState();
    expect(state.contextUsage).toMatchObject({ used: 42, limit: 100 });
    expect(state.totalBudgetPercent).toBe(0.42);
    expect(state.budgetBuckets.map((bucket) => [bucket.name, bucket.used])).toEqual([
      ["prompt", 30],
      ["tools", 12],
    ]);
    expect(state.budgetBuckets.map((bucket) => bucket.limit)).toEqual([100, 100]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "budget",
        targetId: "budget:conv-active",
        payload: expect.objectContaining({
          percent: 0.42,
          breakdown: { prompt: 30, tools: 12 },
        }),
      }),
    ]);
  });

  it("retains budget warnings in Inspector without repeating replayed toasts", () => {
    expect(handleRuntimeEvent({
      type: "budget.warning",
      conversation_id: "conv-active",
      bucket: "context",
      percent: 0.9,
      will_compact: true,
      replayed: true,
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(pushToast).not.toHaveBeenCalled();
    expect(useAppStore.getState().totalBudgetPercent).toBe(0.9);
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "budget",
        targetId: "budget-warning:conv-active:context",
        payload: expect.objectContaining({
          percent: 0.9,
          will_compact: true,
          replayed: true,
        }),
      }),
    ]);
  });

  it("keeps the observable context ledger from usage events", () => {
    expect(handleRuntimeEvent({
      type: "context_usage",
      conversation_id: "conv-active",
      used: 1200,
      limit: 8000,
      ledger: {
        estimated_tokens: 1100,
        actual_tokens: 1200,
        compaction_count: 0,
        entries: [{
          category: "history",
          label: "History",
          estimated_tokens: 700,
          item_count: 4,
          source_count: 0,
          sources: [],
        }],
      },
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(useAppStore.getState().contextUsage?.ledger?.entries[0]?.category).toBe("history");
  });
});

describe("runtime task update events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      todos: [],
      runtimeSession: null,
      permissionMode: "confirm",
      pendingApproval: null,
      pendingAskUser: null,
    });
  });

  it("stores session runtime snapshots and preserves Plan permission mode", () => {
    expect(handleRuntimeEvent({
      type: "task.update",
      session: {
        session_id: "session-runtime",
        permission_mode: "plan",
        pending_approval_count: 2,
        pending_approvals: [
          {
            request_id: "approval-1",
            type: "approval_request",
            conversation_id: "conv-runtime",
            tool_name: "write_file",
          },
          {
            request_id: "ask-1",
            type: "ask_user",
            conversation_id: "conv-runtime",
            subtype: "elicitation",
          },
        ],
      },
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.todos).toEqual([]);
    expect(state.permissionMode).toBe("plan");
    expect(state.runtimeSession?.pending_approval_count).toBe(2);
    expect(state.runtimeSession?.pending_approvals?.map((item) => item.request_id)).toEqual([
      "approval-1",
      "ask-1",
    ]);
  });

  it("still handles legacy todo task updates", () => {
    expect(handleRuntimeEvent({
      type: "task.update",
      todo_id: "todo-1",
      status: "in_progress",
      content: "Run tests",
      activeForm: "testing",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().todos).toEqual([
      {
        id: "todo-1",
        status: "in_progress",
        content: "Run tests",
        activeForm: "testing",
      },
    ]);
  });

  it("applies todo snapshots from task updates", () => {
    useAppStore.setState({
      todos: [
        {
          id: "todo-stale",
          status: "completed",
          content: "Old task",
          activeForm: "Old task",
        },
      ],
    });

    expect(handleRuntimeEvent({
      type: "task.update",
      todos: [],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().todos).toEqual([]);
  });

  it("dedupes repeated todo rows from a full snapshot", () => {
    expect(handleRuntimeEvent({
      type: "task.update",
      todos: [
        {
          id: "todo-a",
          status: "pending",
          content: "Run tests",
          activeForm: "Running tests",
        },
        {
          id: "todo-b",
          status: "in_progress",
          content: "Run tests",
          activeForm: "Running the focused test suite",
        },
      ],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().todos).toEqual([
      {
        id: "todo-b",
        status: "in_progress",
        content: "Run tests",
        activeForm: "Running the focused test suite",
      },
    ]);
  });

  it("keeps a same-turn todo snapshot from rolling visible progress backward", () => {
    useAppStore.setState({
      todos: [
        {
          id: "old-1",
          status: "completed",
          content: "Inspect existing files",
          activeForm: "Inspecting existing files",
        },
        {
          id: "old-2",
          status: "completed",
          content: "Remove old files",
          activeForm: "Removing old files",
        },
        {
          id: "old-3",
          status: "in_progress",
          content: "Rewrite the plan",
          activeForm: "Rewriting the plan",
        },
        {
          id: "old-4",
          status: "pending",
          content: "Verify the result",
          activeForm: "Verifying the result",
        },
      ],
    });

    expect(handleRuntimeEvent({
      type: "task.update",
      todos: [
        {
          id: "new-1",
          status: "in_progress",
          content: "Inspect current Weizhou Island plan files",
          activeForm: "Inspecting current Weizhou Island plan files",
        },
        {
          id: "new-2",
          status: "pending",
          content: "Remove all existing Weizhou Island plan files",
          activeForm: "Removing all existing Weizhou Island plan files",
        },
        {
          id: "new-3",
          status: "pending",
          content: "Rewrite a single Weizhou Island Markdown plan",
          activeForm: "Rewriting a single Weizhou Island Markdown plan",
        },
        {
          id: "new-4",
          status: "pending",
          content: "Verify the rewritten Markdown plan",
          activeForm: "Verifying the rewritten Markdown plan",
        },
      ],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().todos).toEqual([
      expect.objectContaining({
        id: "new-1",
        status: "completed",
        content: "Inspect current Weizhou Island plan files",
      }),
      expect.objectContaining({
        id: "new-2",
        status: "completed",
        content: "Remove all existing Weizhou Island plan files",
      }),
      expect.objectContaining({
        id: "new-3",
        status: "in_progress",
        content: "Rewrite a single Weizhou Island Markdown plan",
      }),
      expect.objectContaining({
        id: "new-4",
        status: "pending",
        content: "Verify the rewritten Markdown plan",
      }),
    ]);
  });

  it("ignores identical todo task updates without replacing todo state", () => {
    useAppStore.setState({
      todos: [
        {
          id: "todo-1",
          status: "in_progress",
          content: "Run tests",
          activeForm: "testing",
        },
      ],
    });
    const before = useAppStore.getState().todos;

    expect(handleRuntimeEvent({
      type: "task.update",
      todo_id: "todo-1",
      status: "in_progress",
      content: "Run tests",
      activeForm: "testing",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().todos).toBe(before);
  });

  it("does not write debug logs for task updates", () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});

    expect(handleRuntimeEvent({
      type: "task.update",
      todo_id: "todo-quiet",
      status: "pending",
      content: "Keep the console clean",
    } as unknown as ServerEvent)).toBe(true);

    expect(logSpy).not.toHaveBeenCalled();
    logSpy.mockRestore();
  });
});

describe("runtime capability catalog hydration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      runtimeCapabilities: null,
      availableSkills: [],
      slashCommands: [],
    });
  });

  it("hydrates composer skills and slash commands from runtime.capabilities", () => {
    expect(handleRuntimeEvent({
      type: "runtime.capabilities",
      capabilities: {
        skills: [{
          name: "openai-docs",
          description: "Use official docs",
          display_name: "OpenAI Docs",
          source_level: "builtin",
          allow_implicit_invocation: false,
          mcp_dependencies: ["docs"],
        }],
        composer_commands: [
          {
            name: "goal",
            command: "goal",
            label: "/goal",
            description: "Manage the goal",
            type: "local",
            args: [{ value: "pause", description: "Pause goal" }],
          },
          {
            name: "disabled",
            command: "disabled",
            label: "/disabled",
            description: "Hidden",
            type: "local",
            enabled: false,
          },
        ],
      },
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.availableSkills).toMatchObject([{
      name: "openai-docs",
      display_name: "OpenAI Docs",
      source_level: "builtin",
      allow_implicit_invocation: false,
      mcp_dependencies: ["docs"],
    }]);
    expect(state.slashCommands).toEqual([{
      name: "goal",
      command: "goal",
      label: "/goal",
      description: "Manage the goal",
      type: "local",
      args: [{ value: "pause", description: "Pause goal" }],
    }]);
  });
});

describe("runtime agent progress events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-runtime",
      agentProgress: [],
      inspectorEntries: [],
      messages: [{
        id: "assistant-runtime",
        role: "assistant",
        content: "",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
    });
  });

  it("keeps tool-owned progress out of the transcript but projects it to Inspector", () => {
    expect(handleRuntimeEvent({
      type: "agent.progress",
      id: "tool:read-1",
      stage: "tool",
      phase: "tool",
      status: "running",
      message: "Queued read_file",
      summary: "Queued read_file",
      visibility: "timeline",
      tool_call_id: "read-1",
      tool_name: "read_file",
      message_id: "assistant-runtime",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.agentProgress).toEqual([]);
    expect(state.messages[0]?.blocks ?? []).toEqual([]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "tool_call",
        targetId: "read-1",
        payload: expect.objectContaining({
          event: "agent.progress",
          stage: "tool",
          status: "running",
          message: "Queued read_file",
          tool_name: "read_file",
        }),
      }),
    ]);
  });

  it("keeps approval progress available as tool-call diagnostics", () => {
    expect(handleRuntimeEvent({
      type: "agent.progress",
      id: "approval:write-1",
      stage: "approval",
      phase: "approval",
      status: "running",
      message: "Waiting for approval: write_file",
      summary: "Waiting for approval: write_file",
      visibility: "timeline",
      tool_call_id: "write-1",
      tool_name: "write_file",
      message_id: "assistant-runtime",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.agentProgress).toEqual([]);
    expect(state.messages[0]?.blocks ?? []).toEqual([]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "tool_call",
        targetId: "write-1",
        payload: expect.objectContaining({
          event: "agent.progress",
          stage: "approval",
          message: "Waiting for approval: write_file",
        }),
      }),
    ]);
  });

  it("keeps runtime spans in the inspector without synthesizing progress", () => {
    expect(handleRuntimeEvent({
      type: "runtime.span",
      event: "tool.started",
      span_id: "tool:read-1",
      phase: "tool",
      status: "running",
      summary: "Running read_file",
      tool_call_id: "read-1",
      tool_name: "read_file",
      iteration_id: "iter:1",
      message_id: "assistant-runtime",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.agentProgress).toEqual([]);
    expect(state.messages[0]?.blocks ?? []).toEqual([]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "tool_call",
        targetId: "read-1",
        payload: expect.objectContaining({
          event: "runtime.span",
          span_id: "tool:read-1",
          tool_call_id: "read-1",
        }),
      }),
    ]);
  });

  it("keeps subagent runtime spans in the inspector without synthesizing collaboration state", () => {
    expect(handleRuntimeEvent({
      type: "runtime.span",
      event: "subagent.progress",
      span_id: "subagent:agent-1",
      phase: "subagent",
      status: "running",
      summary: "Reviewing backend events",
      agent_id: "agent-1",
      ui_visible: false,
      message_id: "assistant-runtime",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.agentProgress).toEqual([]);
    expect(state.messages[0]?.blocks ?? []).toEqual([]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "subagent",
        targetId: "agent-1",
      }),
    ]);
  });

  it("does not let diagnostic spans own subagent terminal state", () => {
    useAppStore.setState({
      subagents: [{
        id: "task-weather",
        role: "research",
        status: "running",
        taskId: "task-weather",
        objective: "查询广州天气",
      }],
    });

    expect(handleRuntimeEvent({
      type: "runtime.span",
      event: "subagent.completed",
      span_id: "subagent:worker-weather",
      phase: "subagent",
      status: "completed",
      summary: "广州天气查询完成",
      agent_id: "worker-weather",
      data: { task_id: "task-weather" },
      ui_visible: false,
      message_id: "assistant-runtime",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({ id: "task-weather", status: "running" }),
    ]);
  });

  it("does not let failed diagnostic spans overwrite subagent state", () => {
    useAppStore.setState({
      subagents: [{ id: "worker-failed", role: "research", status: "running" }],
    });

    expect(handleRuntimeEvent({
      type: "runtime.span",
      event: "subagent.failed",
      span_id: "subagent:worker-failed",
      phase: "subagent",
      status: "failed",
      summary: "天气服务不可用",
      agent_id: "worker-failed",
      data: { error: "upstream unavailable" },
      ui_visible: false,
      message_id: "assistant-runtime",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(useAppStore.getState().subagents[0]).toEqual({
      id: "worker-failed",
      role: "research",
      status: "running",
    });
  });

  it("keeps ordinary cache runtime spans debug-only while preserving cache inspector payloads", () => {
    expect(handleRuntimeEvent({
      type: "runtime.span",
      event: "cache.lookup.hit",
      span_id: "cache:provider.prompt:sig",
      phase: "cache",
      status: "completed",
      summary: "Cache hit: provider.prompt",
      ui_visible: false,
      message_id: "assistant-runtime",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.agentProgress).toEqual([]);
    expect(state.messages[0]?.blocks ?? []).toEqual([]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "cache",
        targetId: "cache:provider.prompt:sig",
      }),
    ]);
  });
});

describe("provider progress ownership and recovery", () => {
  const providerEvent = (overrides: Record<string, unknown> = {}) => ({
    type: "agent.progress",
    conversation_id: "conv-provider",
    message_id: "assistant-provider",
    id: "provider:connection:run-provider:iteration-1",
    stage: "status",
    phase: "model",
    status: "running",
    message: "正在重连 1/10",
    summary: "正在重连 1/10",
    visibility: "timeline",
    retry_attempt: 1,
    max_retries: 10,
    provider_state: "reconnecting",
    ...overrides,
  } as unknown as ServerEvent);

  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-provider",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      pendingProviderProgress: {},
      agentProgress: [],
      inspectorEntries: [],
      isStreaming: false,
    });
  });

  it("replays a provider frame that arrived before the assistant placeholder", () => {
    expect(handleRuntimeEvent(providerEvent())).toBe(true);
    expect(useAppStore.getState().pendingProviderProgress).toEqual({
      "conv-provider\u0000assistant-provider": [
        expect.objectContaining({
          id: "provider:connection:run-provider:iteration-1",
          retryAttempt: 1,
          maxRetries: 10,
        }),
      ],
    });
    expect(useAppStore.getState().inspectorEntries).toEqual([]);

    useAppStore.getState().resumeStreaming(
      "conv-provider",
      [],
      "assistant-provider",
      "turn-provider",
    );

    const state = useAppStore.getState();
    expect(state.pendingProviderProgress).toEqual({});
    expect(state.messages[0]).toMatchObject({
      id: "assistant-provider",
      isStreaming: true,
      blocks: [expect.objectContaining({
        type: "progress",
        id: "provider:connection:run-provider:iteration-1",
        retryAttempt: 1,
        maxRetries: 10,
      })],
    });
    expect(state.agentProgress).toEqual([
      expect.objectContaining({
        id: "provider:connection:run-provider:iteration-1",
        retryAttempt: 1,
        maxRetries: 10,
      }),
    ]);
  });

  it("does not cross-wire equal message ids between conversations or different messages", () => {
    expect(handleRuntimeEvent(providerEvent({
      conversation_id: "conv-one",
      message_id: "same-message",
      message: "one",
    }), "conv-one")).toBe(true);
    expect(handleRuntimeEvent(providerEvent({
      conversation_id: "conv-one",
      message_id: "other-message",
      message: "other",
    }), "conv-one")).toBe(true);
    expect(handleRuntimeEvent(providerEvent({
      conversation_id: "conv-two",
      message_id: "same-message",
      message: "two",
    }), "conv-two")).toBe(true);

    useAppStore.getState().resumeStreaming("conv-one", [], "same-message", "turn-one");
    const state = useAppStore.getState();
    expect(state.conversationMessages["conv-one"]).toEqual([
      expect.objectContaining({
        id: "same-message",
        blocks: [expect.objectContaining({ message: "one" })],
      }),
    ]);
    expect(state.pendingProviderProgress).toEqual({
      "conv-one\u0000other-message": [expect.objectContaining({ message: "other" })],
      "conv-two\u0000same-message": [expect.objectContaining({ message: "two" })],
    });
  });

  it("upserts one provider retry row and never reopens it after terminal completion", () => {
    useAppStore.setState({
      messages: [{
        id: "assistant-provider",
        role: "assistant",
        content: "",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      conversationMessages: { "conv-provider": [] },
      conversationStreaming: { "conv-provider": true },
      isStreaming: true,
    });

    expect(handleRuntimeEvent(providerEvent())).toBe(true);
    expect(handleRuntimeEvent(providerEvent({
      retry_attempt: 2,
      message: "正在重连 2/10",
      summary: "正在重连 2/10",
    }))).toBe(true);
    useAppStore.getState().finishStreaming(
      "conv-provider",
      undefined,
      "failed",
      "assistant-provider",
      "provider unavailable",
    );

    expect(handleRuntimeEvent(providerEvent({
      status: "running",
      retry_attempt: 1,
      message: "正在重连 1/10",
    }))).toBe(true);
    const state = useAppStore.getState();
    const assistant = state.messages[0];
    expect(assistant?.terminalStatus).toBe("failed");
    expect(assistant?.blocks).toEqual([
      expect.objectContaining({
        type: "progress",
        id: "provider:connection:run-provider:iteration-1",
        status: "failed",
        retryAttempt: 2,
        maxRetries: 10,
      }),
    ]);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "provider",
        payload: expect.objectContaining({
          dropped: true,
          reason: "turn_already_terminal",
        }),
      }),
    ]);
  });
});

describe("runtime session terminal fallback", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-runtime-terminal",
      messages: [{
        id: "assistant-runtime-terminal",
        role: "assistant",
        content: "完成。",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-runtime-terminal": true },
      isStreaming: true,
      agentProgress: [],
    });
  });

  it("idle releases busy state without guessing an explicit completed terminal", () => {
    expect(handleRuntimeEvent({
      type: "session.state_changed",
      state: "idle",
      conversation_id: "conv-runtime-terminal",
      reason: "completed",
    } as unknown as ServerEvent, "conv-runtime-terminal")).toBe(true);

    const state = useAppStore.getState();
    expect(state.messages[0]).toMatchObject({
      id: "assistant-runtime-terminal",
      isStreaming: true,
    });
    expect(state.messages[0]?.terminalStatus).toBeUndefined();
    expect(state.conversationStreaming["conv-runtime-terminal"]).toBe(false);
    expect(state.isStreaming).toBe(false);
  });

  it("unqualified idle also leaves message terminal ownership to done", () => {
    expect(handleRuntimeEvent({
      type: "session.state_changed",
      state: "idle",
      conversation_id: "conv-runtime-terminal",
    } as unknown as ServerEvent, "conv-runtime-terminal")).toBe(true);

    expect(useAppStore.getState().messages[0]).toMatchObject({
      id: "assistant-runtime-terminal",
      isStreaming: true,
    });
    expect(useAppStore.getState().messages[0]?.terminalStatus).toBeUndefined();
  });

  it("accepts an authoritative partial done that arrives after idle", () => {
    expect(handleRuntimeEvent({
      type: "session.state_changed",
      state: "idle",
      conversation_id: "conv-runtime-terminal",
    } as unknown as ServerEvent, "conv-runtime-terminal")).toBe(true);

    const immediateBuffer: StreamBuffer = {
      push: vi.fn(),
      flush: vi.fn(),
      destroy: vi.fn(),
    };
    expect(handleChatStreamEvent({
      type: "done",
      status: "partial",
      reason: "max_iterations",
      conversation_id: "conv-runtime-terminal",
      message_id: "assistant-runtime-terminal",
    } as unknown as ServerEvent, "conv-runtime-terminal", {
      textStreamBuffer: immediateBuffer,
      thinkingStreamBuffer: immediateBuffer,
    })).toBe(true);

    expect(useAppStore.getState().messages[0]).toMatchObject({
      id: "assistant-runtime-terminal",
      isStreaming: false,
      terminalStatus: "partial",
    });
  });

  it("uses durable agent completion to seal a still-streaming assistant", () => {
    useAppStore.setState({
      messages: [{
        id: "assistant-runtime-terminal",
        role: "assistant",
        content: "partial output",
        blocks: [{ type: "text", itemId: "item-1", content: "partial output", status: "in_progress", isStreaming: true }],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
        isThinkingStreaming: false,
      }],
      conversationStreaming: { "conv-runtime-terminal": true },
      conversationMessages: {},
      isStreaming: true,
    });
    expect(handleRuntimeEvent({
      type: "agent.run.completed",
      conversation_id: "conv-runtime-terminal",
      message_id: "assistant-runtime-terminal",
      run_id: "run-partial",
      status: "partial",
      summary: "Reached the turn budget",
      terminal_reason: "max_iterations",
    } as unknown as ServerEvent, "conv-runtime-terminal")).toBe(true);

    expect(useAppStore.getState().agentProgress).toEqual([]);
    expect(useAppStore.getState().inspectorEntries).toContainEqual(expect.objectContaining({
      targetKind: "message",
      targetId: "assistant-runtime-terminal",
      payload: expect.objectContaining({ type: "agent.run.completed", status: "partial" }),
    }));
    expect(useAppStore.getState().messages[0]).toMatchObject({
      isStreaming: false,
      terminalStatus: "partial",
      terminationReason: "max_iterations",
    });
  });
});

describe("runtime capability events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      runtimeCapabilities: null,
    });
  });

  it("stores the session capability snapshot from runtime.capabilities", () => {
    useAppStore.setState({
      quickOpenVisible: true,
      quickOpenResults: [{ name: "app.tsx", path: "src/app.tsx" }],
      quickOpenLoading: true,
      agentEditorOpen: true,
    });

    expect(handleRuntimeEvent({
      type: "runtime.capabilities",
      session_id: "session-capabilities",
      capabilities: {
        tools: [{ type: "function", function: { name: "read_file" } }],
        tool_views: [
          { name: "read_file", exposure: "core", direct: true, schema_available: true },
          { name: "tool_call", exposure: "deferred", direct: false, schema_available: true },
        ],
        summary: {
          deferred_bridge: true,
        },
        feature_flags: {
          global_search: { enabled: false, source: "settings" },
          agent_editor: { enabled: false, source: "settings" },
        },
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().runtimeCapabilities).toMatchObject({
      summary: {
        tools_total: 2,
        direct_tools: 1,
        core_tools: 1,
        deferred_tools: 1,
        deferred_bridge: true,
      },
      feature_flags: {
        global_search: { enabled: false, source: "settings" },
      },
    });
    expect(useAppStore.getState().quickOpenVisible).toBe(false);
    expect(useAppStore.getState().quickOpenResults).toEqual([]);
    expect(useAppStore.getState().quickOpenLoading).toBe(false);
    expect(useAppStore.getState().agentEditorOpen).toBe(false);
  });
});

describe("runtime subagent events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-runtime",
      subagents: [],
      rightStackTab: "preview",
      rightStackTabLocked: false,
    });
  });

  it("stores backend subagent.progress events as visible subagent progress", () => {
    expect(handleRuntimeEvent({
      type: "subagent.start",
      subagent_id: "subagent-1",
      parent_id: "root",
      role: "research",
    } as unknown as ServerEvent)).toBe(true);

    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-1",
      iteration: 2,
      max_iterations: 5,
      tool_name: "read_file",
      tool_call_id: "call-readme",
      source_event_type: "tool_call",
      detail: "Reading README.md",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({
        id: "subagent-1",
        role: "research",
        status: "running",
        summary: "Reading README.md",
        iteration: 2,
        maxIterations: 5,
        currentTool: "read_file",
        currentToolCallId: "call-readme",
        progressSource: "tool_call",
        activityLog: ["Reading README.md"],
      }),
    ]);
  });

  it("keeps refreshed pending and blocked subagents non-terminal", () => {
    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-waiting",
      status: "pending",
      detail: "Waiting for a worker slot",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents[0]).toEqual(expect.objectContaining({
      id: "subagent-waiting",
      status: "pending",
    }));

    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-waiting",
      status: "blocked",
      detail: "Waiting for approval",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents[0]).toEqual(expect.objectContaining({
      id: "subagent-waiting",
      status: "blocked",
      summary: "Waiting for approval",
    }));
  });

  it("renders structured subagent activity without tool-name rewriting", () => {
    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-structured",
      tool_name: "read_file",
      detail: "call_internal_12345678",
      activity_kind: "narration",
      activity_summary: "正在核对权限链路",
      user_visible: true,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents[0]).toEqual(expect.objectContaining({
      summary: "正在核对权限链路",
      currentActivity: "正在核对权限链路",
      activityLog: ["正在核对权限链路"],
    }));
  });

  it("keeps explicit protocol activity for delegated work", () => {
    expect(handleRuntimeEvent({
      type: "subagent.start",
      subagent_id: "subagent-weather",
      role: "research",
      prompt: "查询北京今天天气",
    } as unknown as ServerEvent)).toBe(true);
    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-weather",
      tool_name: "web_search",
      current_activity: "查询北京今天天气",
    } as unknown as ServerEvent)).toBe(true);
    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-weather",
      tool_name: "web_fetch",
      current_activity: "查询北京今天天气",
    } as unknown as ServerEvent)).toBe(true);
    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-weather",
      summary: "北京天气已查询完成",
      result: { status: "completed", content: "北京今天小雨转多云。" },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents[0]?.activityLog).toEqual([
      "查询北京今天天气",
    ]);
  });

  it("does not revive a terminal subagent when delayed progress arrives", () => {
    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-terminal",
      summary: "石家庄天气已完成",
      result: {
        status: "completed",
        content: "天气结果",
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-terminal",
      iteration: 3,
      max_iterations: 5,
      current_activity: "Running call_stale123456789",
      last_progress_at: Date.now() + 10_000,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({
        id: "subagent-terminal",
        status: "done",
        summary: "石家庄天气已完成",
        resultContent: "天气结果",
      }),
    ]);
  });

  it("does not erase a terminal result when a delayed start arrives", () => {
    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-late-start",
      summary: "FACT_2=value-34",
      result: { status: "completed", content: "FACT_2=value-34" },
    } as unknown as ServerEvent)).toBe(true);

    expect(handleRuntimeEvent({
      type: "subagent.start",
      subagent_id: "subagent-late-start",
      role: "subagent",
      prompt: "Read fact-2.txt",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({
        id: "subagent-late-start",
        status: "done",
        resultContent: "FACT_2=value-34",
      }),
    ]);
  });

  it("ignores duplicate terminal reconciliation events", () => {
    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-duplicate",
      summary: "天气已完成",
      result: { status: "completed", content: "广州天气结果" },
    } as unknown as ServerEvent)).toBe(true);

    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-duplicate",
      snapshot: { result: { status: "completed", content: "广州天气结果" } },
      result: { status: "completed", content: "广州天气结果" },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents[0]?.activityLog).toEqual([]);
  });

  it("does not reconstruct subagents from nested task snapshots", () => {
    expect(handleRuntimeEvent({
      type: "subagent.event",
      subagent_id: "task-partial",
      event: {
        type: "task_updated",
        task: {
          task_id: "task-partial",
          title: "查询北京天气",
          status: "partial",
        },
      },
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);
    expect(handleRuntimeEvent({
      type: "subagent.event",
      subagent_id: "task-cancelled",
      event: {
        type: "task_updated",
        task: {
          task_id: "task-cancelled",
          title: "查询上海天气",
          status: "cancelled",
        },
      },
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([]);
  });

  it("keeps raw iteration counters out of user-visible subagent summaries", () => {
    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-iteration",
      iteration: 2,
      max_iterations: 5,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({
        id: "subagent-iteration",
          summary: "正在执行子任务",
        iteration: 2,
        maxIterations: 5,
      }),
    ]);
    expect(JSON.stringify(useAppStore.getState().subagents)).not.toContain("Iteration 2/5");
  });

  it("stores swarm messages without replacing the task shown in compact surfaces", () => {
    useAppStore.setState({
      subagents: [{
        id: "subagent-audit",
        role: "reviewer",
        status: "running",
        summary: "Reviewing parser",
      }],
    });

    expect(handleRuntimeEvent({
      type: "subagent.event",
      subagent_id: "subagent-audit",
      event: {
        type: "message",
        message: {
          message_id: "msg-steer-1",
          sender_id: "parent-run",
          recipient_id: "subagent-audit",
          content: "Please verify the parser.",
          sender_mailbox_epoch: 0,
          recipient_mailbox_epoch: 3,
        },
      },
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({
        id: "subagent-audit",
        role: "reviewer",
        status: "running",
        summary: "Reviewing parser",
        messages: [expect.objectContaining({
          messageId: "msg-steer-1",
          content: "Please verify the parser.",
          deliveryStatus: "sent",
          senderMailboxEpoch: 0,
          recipientMailboxEpoch: 3,
        })],
      }),
    ]);
    expect(useAppStore.getState().subagents[0]?.currentActivity).toBeUndefined();
    expect(useAppStore.getState().subagents[0]?.detail).toBeUndefined();
  });

  it("ignores orphan swarm messages in the primary collaboration list", () => {
    expect(handleRuntimeEvent({
      type: "subagent.event",
      subagent_id: "subagent-audit",
      event: {
        type: "message",
        message: {
          message_id: "msg-orphan-1",
          sender_id: "parent-run",
          recipient_id: "subagent-audit",
          content: "Please verify the parser.",
        },
      },
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([]);
  });

  it("keeps the original task objective after a worker fails", () => {
    expect(handleRuntimeEvent({
      type: "subagent.start",
      subagent_id: "subagent-weather",
      parent_id: "root",
      role: "explore",
      prompt: "调研成都天气",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-weather",
      error: "RuntimeError: 已达到最大迭代次数限制（12次）。",
      result: {
        status: "failed",
        error: "RuntimeError: 已达到最大迭代次数限制（12次）。",
      },
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(useAppStore.getState().subagents[0]).toMatchObject({
      objective: "调研成都天气",
      summary: "RuntimeError: 已达到最大迭代次数限制（12次）。",
      status: "error",
    });
  });

  it("stores collected subagent results from status snapshots", () => {
    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-result",
      summary: "Finished",
      snapshot: {
        result: {
          content: "Full subagent answer",
          duration_ms: 1234,
          tool_call_count: 3,
        },
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({
        id: "subagent-result",
        status: "done",
        summary: "Finished",
        resultContent: "Full subagent answer",
        durationMs: 1234,
        toolCallCount: 3,
      }),
    ]);
  });

  it("preserves timed-out subagent results as partial deadline outcomes", () => {
    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-timeout",
      summary: "Subagent timed out after 300s.",
      timed_out: true,
      result: {
        status: "failed",
        content: "Subagent subagent-timeout timed out after 300s with no result.",
        duration_ms: 300000,
        tool_call_count: 2,
        timed_out: true,
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([
      expect.objectContaining({
        id: "subagent-timeout",
        status: "partial",
        terminationReason: "deadline_exceeded",
        summary: "Subagent timed out after 300s.",
        resultContent: "Subagent subagent-timeout timed out after 300s with no result.",
        durationMs: 300000,
        toolCallCount: 2,
        resultAvailable: true,
      }),
    ]);
  });

  it("ignores phantom parallel-batch done events", () => {
    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "parallel-batch",
      error: "outer timeout",
      duration_ms: 5000,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().subagents).toEqual([]);
  });

  it("accepts only increasing child transcript snapshots and keeps the terminal replay", () => {
    expect(handleRuntimeEvent({
      type: "subagent.start",
      subagent_id: "subagent-transcript",
      role: "reviewer",
      prompt: "检查实现",
    } as unknown as ServerEvent)).toBe(true);

    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-transcript",
      transcript_snapshot: {
        seq: 2,
        messages: [
          { id: "child-user", role: "user", content: "检查实现", timestamp: 1 },
          {
            id: "child-tool",
            role: "assistant",
            content: "",
            timestamp: 2,
            is_streaming: true,
            blocks: [{
              type: "tool_call",
              record: {
                id: "call-read",
                name: "read_file",
                args: { path: "README.md" },
                status: "running",
              },
            }],
          },
        ],
      },
    } as unknown as ServerEvent)).toBe(true);

    let child = useAppStore.getState().subagents[0];
    expect(child.transcriptSeq).toBe(2);
    expect(child.transcriptMessages).toHaveLength(2);
    expect(child.transcriptMessages?.[1]?.blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: { id: "call-read", status: "running" },
    });

    expect(handleRuntimeEvent({
      type: "subagent.progress",
      subagent_id: "subagent-transcript",
      transcript_snapshot: {
        seq: 1,
        messages: [{ id: "stale", role: "assistant", content: "stale", timestamp: 1 }],
      },
    } as unknown as ServerEvent)).toBe(true);
    child = useAppStore.getState().subagents[0];
    expect(child.transcriptSeq).toBe(2);
    expect(child.transcriptMessages?.some((message) => message.content === "stale")).toBe(false);

    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-transcript",
      summary: "检查完成",
      result: { status: "completed", content: "最终结果" },
      transcript_snapshot: {
        seq: 5,
        messages: [
          { id: "child-user", role: "user", content: "检查实现", timestamp: 1 },
          {
            id: "child-tool",
            role: "assistant",
            content: "",
            timestamp: 2,
            blocks: [{
              type: "tool_call",
              record: {
                id: "call-read",
                name: "read_file",
                args: { path: "README.md" },
                status: "success",
                output_preview: "file contents",
              },
            }],
          },
          {
            id: "child-final",
            role: "assistant",
            content: "最终结果",
            timestamp: 3,
            completed_at: 4,
            duration_ms: 3,
            terminal_status: "completed",
            termination_reason: "success",
            is_streaming: false,
          },
        ],
      },
    } as unknown as ServerEvent)).toBe(true);

    child = useAppStore.getState().subagents[0];
    expect(child.status).toBe("done");
    expect(child.transcriptSeq).toBe(5);
    expect(child.transcriptMessages?.at(-1)?.content).toBe("最终结果");
    expect(child.transcriptMessages?.at(-1)).toMatchObject({
      isStreaming: false,
      terminalStatus: "completed",
    });
    expect(child.transcriptMessages?.[1]?.blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: { id: "call-read", status: "success", outputPreview: "file contents" },
    });
  });

  it("does not invent child transcript terminal state when done has no durable snapshot", () => {
    expect(handleRuntimeEvent({
      type: "subagent.start",
      subagent_id: "subagent-terminalize",
      role: "reviewer",
      prompt: "检查实现",
      transcript_snapshot: {
        seq: 2,
        messages: [{
          id: "child-live",
          role: "assistant",
          content: "仍在处理",
          timestamp: 1,
          is_streaming: true,
        }],
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(handleRuntimeEvent({
      type: "subagent.done",
      subagent_id: "subagent-terminalize",
      status: "failed",
      error: "RuntimeError: 子任务未完成",
      duration_ms: 78_170,
    } as unknown as ServerEvent)).toBe(true);

    const child = useAppStore.getState().subagents[0];
    expect(child.durationMs).toBe(78_170);
    expect(child.transcriptMessages?.at(-1)).toMatchObject({
      isStreaming: true,
      content: "仍在处理",
    });
    expect(child.transcriptMessages?.at(-1)?.terminalStatus).toBeUndefined();
    expect(child.transcriptMessages?.at(-1)?.failureMessage).toBeUndefined();
  });
});

describe("runtime plan snapshot events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-plan",
      messages: [{
        id: "assistant-plan",
        role: "assistant",
        content: "",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        turnId: "turn-1",
        isStreaming: true,
      }],
      plan: null,
    });
  });

  it("replaces the plan from an explicit turn-scoped snapshot", () => {
    expect(handleRuntimeEvent({
      type: "turn.plan.updated",
      thread_id: "conv-plan",
      conversation_id: "conv-plan",
      turn_id: "turn-1",
      plan: [
        { step: "Read", status: "completed" },
        { step: "Test", status: "in_progress" },
      ],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().plan).toEqual({
      threadId: "conv-plan",
      turnId: "turn-1",
      plan: [
        { step: "Read", status: "completed" },
        { step: "Test", status: "in_progress" },
      ],
    });
  });
});

describe("runtime mcp lifecycle/progress events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      messages: [],
      mcpServers: [{ name: "demo", status: "connected", phase: "connected" }],
    });
  });

  it("folds mcp.lifecycle into the connector without treating it as chat text", () => {
    const handled = handleRuntimeEvent({
      type: "mcp.lifecycle",
      server_name: "demo",
      status: "error",
      phase: "auth_required",
      message: "Authentication required",
      auth_status: "not_logged_in",
      recoverable: false,
      requires_user_action: true,
      setup_hint: "Open Figma Desktop and enable the Dev Mode MCP server.",
      docs_url: "https://help.figma.com/",
    } as unknown as ServerEvent);

    expect(handled).toBe(true);
    const server = useAppStore.getState().mcpServers.find((s) => s.name === "demo");
    expect(server).toMatchObject({
      phase: "auth_required",
      authStatus: "not_logged_in",
      requiresUserAction: true,
      recoverable: false,
      lastError: "Authentication required",
      setupHint: "Open Figma Desktop and enable the Dev Mode MCP server.",
      docsUrl: "https://help.figma.com/",
    });
    // Consumed by the runtime reducer => never appended to the chat transcript.
    expect(useAppStore.getState().messages).toHaveLength(0);
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "mcp.lifecycle",
    });
  });

  it("adds a previously-unknown connector from a lifecycle event", () => {
    expect(handleRuntimeEvent({
      type: "mcp.lifecycle",
      server_name: "fresh",
      status: "reconnecting",
      phase: "reconnecting",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().mcpServers.find((s) => s.name === "fresh")?.phase).toBe("reconnecting");
  });

  it("records compact connect progress on the matching connector", () => {
    expect(handleRuntimeEvent({
      type: "mcp.progress",
      server_name: "demo",
      operation: "connect",
      message: "Connecting…",
      status: "running",
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().mcpServers.find((s) => s.name === "demo")?.progress).toMatchObject({
      operation: "connect",
      status: "running",
    });
  });
});

describe("runtime plan_updated events", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-runtime",
      messages: [{
        id: "assistant-plan",
        role: "assistant",
        content: "",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        turnId: "turn-sess1",
        isStreaming: true,
      }],
      conversationMessages: {},
      plan: null,
      agentProgress: [],
    });
  });

  it("creates the live plan from a full turn.plan.updated snapshot", () => {
    expect(handleRuntimeEvent({
      type: "turn.plan.updated",
      thread_id: "conv-runtime",
      conversation_id: "conv-runtime",
      turn_id: "turn-sess1",
      plan: [
        { step: "设计", status: "completed" },
        { step: "实现", status: "in_progress" },
        { step: "测试", status: "pending" },
      ],
    } as unknown as ServerEvent)).toBe(true);

    const plan = useAppStore.getState().plan;
    expect(plan).toMatchObject({ threadId: "conv-runtime", turnId: "turn-sess1" });
    expect(plan?.plan.map((s) => [s.step, s.status])).toEqual([
      ["设计", "completed"],
      ["实现", "in_progress"],
      ["测试", "pending"],
    ]);
    expect(useAppStore.getState().agentProgress).toEqual([]);
  });

  it("stores pending plan updates without appending generic progress", () => {
    expect(handleRuntimeEvent({
      type: "turn.plan.updated",
      thread_id: "conv-runtime",
      conversation_id: "conv-runtime",
      turn_id: "turn-sess1",
      plan: [
        { step: "需求分析", status: "pending" },
        { step: "验收", status: "pending" },
      ],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().plan).toMatchObject({
      threadId: "conv-runtime",
      turnId: "turn-sess1",
      plan: [
        { step: "需求分析", status: "pending" },
        { step: "验收", status: "pending" },
      ],
    });
    expect(useAppStore.getState().agentProgress).toEqual([]);
  });

  it("replaces the plan and advances step status on a later snapshot", () => {
    const send = (statuses: Array<"pending" | "in_progress" | "completed">) =>
      handleRuntimeEvent({
        type: "turn.plan.updated",
        thread_id: "conv-runtime",
        conversation_id: "conv-runtime",
        turn_id: "turn-sess1",
        plan: statuses.map((status, i) => ({ step: `s${i}`, status })),
      } as unknown as ServerEvent);

    send(["in_progress", "pending"]);
    send(["completed", "in_progress"]);

    const plan = useAppStore.getState().plan;
    expect(plan?.turnId).toBe("turn-sess1");
    expect(plan?.plan.map((s) => s.status)).toEqual(["completed", "in_progress"]);
  });

  it("preserves the MiniCode notification payload without rewriting plan text", () => {
    expect(handleRuntimeEvent({
      type: "turn.plan.updated",
      thread_id: "conv-runtime",
      conversation_id: "conv-runtime",
      turn_id: "turn-sess1",
      explanation: "  scope changed  ",
      plan: [
        { step: "", status: "pending" },
        { step: "  inspect exact payload  ", status: "in_progress" },
        { step: "second active step", status: "in_progress" },
      ],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().plan).toEqual({
      threadId: "conv-runtime",
      turnId: "turn-sess1",
      explanation: "  scope changed  ",
      plan: [
        { step: "", status: "pending" },
        { step: "  inspect exact payload  ", status: "in_progress" },
        { step: "second active step", status: "in_progress" },
      ],
    });
  });
});

describe("runtime turn-scoped event isolation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-runtime",
      messages: [
        {
          id: "assistant-cf",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 1,
          turnId: "turn-current",
          isStreaming: true,
        },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-runtime": true },
      plan: null,
      todos: [],
      agentProgress: [],
    });
  });

  it("ignores stale plan and todo updates from a previous assistant message", () => {
    expect(handleRuntimeEvent({
      type: "task.update",
      conversation_id: "conv-runtime",
      message_id: "assistant-angry-birds",
      todo_id: "todo-angry",
      content: "编写愤怒的小鸟 HTML 游戏",
      activeForm: "正在编写愤怒的小鸟 HTML 游戏",
      status: "in_progress",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    expect(handleRuntimeEvent({
      type: "turn.plan.updated",
      thread_id: "conv-runtime",
      conversation_id: "conv-runtime",
      turn_id: "turn-angry-birds",
      message_id: "assistant-angry-birds",
      plan: [
        { step: "用单文件 HTML 实现愤怒的小鸟游戏", status: "in_progress" },
      ],
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.todos).toEqual([]);
    expect(state.plan).toBeNull();
    expect(state.agentProgress).toEqual([]);
  });

  it("accepts terminal runtime events that arrive just after done for the same assistant", () => {
    useAppStore.setState({
      messages: [{
        id: "assistant-cf",
        role: "assistant",
        content: "完成",
        blocks: [{ type: "text", content: "完成", visibility: "final" }],
        artifacts: [],
        timestamp: 1,
        isStreaming: false,
        terminalStatus: "completed",
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-runtime": false },
      isStreaming: false,
      agentProgress: [],
      todos: [],
    });

    expect(handleRuntimeEvent({
      type: "agent.run.completed",
      conversation_id: "conv-runtime",
      message_id: "assistant-cf",
      run_id: "run-cf",
      status: "completed",
      summary: "运行完成",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);
    expect(handleRuntimeEvent({
      type: "task.update",
      conversation_id: "conv-runtime",
      message_id: "assistant-cf",
      todos: [{ todo_id: "todo-1", content: "验证结果", activeForm: "", status: "completed" }],
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const state = useAppStore.getState();
    expect(state.agentProgress).toEqual([]);
    expect(state.todos).toEqual([
      expect.objectContaining({ id: "todo-1", status: "completed", content: "验证结果" }),
    ]);
  });

  // Regression: non-terminal turn-scoped events arriving after `done` cannot be
  // rendered, but they were destroyed with a bare `return true` — including rows
  // carrying status:"failed", which left no record of the failure anywhere.
  it("traces turn-scoped runtime events dropped after the turn's delivery fence", () => {
    useAppStore.setState({
      conversationId: "conv-runtime",
      messages: [{
        id: "assistant-sealed",
        role: "assistant",
        content: "完成",
        blocks: [],
        artifacts: [],
        timestamp: 1,
        isStreaming: false,
        terminalStatus: "completed",
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-runtime": false },
      isStreaming: false,
      agentProgress: [],
      inspectorEntries: [],
    });

    expect(handleRuntimeEvent({
      type: "runtime.span",
      conversation_id: "conv-runtime",
      message_id: "assistant-sealed",
      span_id: "span-late",
      event: "tool.execute",
      status: "failed",
    } as unknown as ServerEvent, "conv-runtime")).toBe(true);

    const entries = useAppStore.getState().inspectorEntries;
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      targetKind: "message",
      targetId: "late:conv-runtime:assistant-sealed:runtime.span",
      payload: {
        event: "runtime.span",
        dropped: true,
        reason: "turn_already_terminal",
        message_id: "assistant-sealed",
        status: "failed",
      },
    });
    expect(useAppStore.getState().agentProgress).toEqual([]);
  });
});

describe("provider control projections", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-active",
      conversationStreaming: { "conv-active": false, "conv-other": false },
      isStreaming: false,
      inspectorEntries: [],
    });
  });

  it("keeps SDK-only provider events out of renderer state and exposes compact metadata for explicit UI events", () => {
    expect(handleRuntimeEvent({
      type: "stream_event",
      conversation_id: "conv-active",
      provider: "openai",
      event_type: "response.output_text.delta",
      data: { delta: "hello", response_id: "response-1" },
      sdk_only: true,
      event_id: "event-sdk",
    } as unknown as ServerEvent, "conv-active")).toBe(true);
    expect(useAppStore.getState().inspectorEntries).toEqual([]);

    expect(handleRuntimeEvent({
      type: "stream_event",
      conversation_id: "conv-active",
      provider: "openai",
      event_type: "provider.notice",
      data: { code: "maintenance", region: "ap-east" },
      sdk_only: false,
      event_id: "event-visible",
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "provider",
        targetId: "stream:conv-active:event-visible",
        payload: {
          event: "stream_event",
          conversation_id: "conv-active",
          provider: "openai",
          event_type: "provider.notice",
          sdk_only: false,
          data_keys: ["code", "region"],
        },
      }),
    ]);
  });

  it("records exact parent notification delivery evidence only for the active owner", () => {
    expect(handleRuntimeEvent({
      type: "parent.notifications",
      conversation_id: "conv-other",
      parent_run_id: "run-other",
      count: 2,
      timestamp: "2026-08-15T10:00:00Z",
    } as unknown as ServerEvent, "conv-other")).toBe(true);
    expect(useAppStore.getState().inspectorEntries).toEqual([]);

    expect(handleRuntimeEvent({
      type: "parent.notifications",
      conversation_id: "conv-active",
      parent_run_id: "run-parent",
      count: 3,
      timestamp: "2026-08-15T10:00:01Z",
      replayed: true,
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "session",
        targetId: "parent-notifications:conv-active:run-parent",
        payload: {
          event: "parent.notifications",
          conversation_id: "conv-active",
          parent_run_id: "run-parent",
          delivered_count: 3,
          replayed: true,
          received_at: Date.parse("2026-08-15T10:00:01Z"),
        },
      }),
    ]);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("shows structured rate-limit detail only for a live event owned by the active conversation", () => {
    expect(handleRuntimeEvent({
      type: "rate_limit",
      conversation_id: "conv-active",
      provider: "supertoken",
      error_type: "rate_limit",
      retry_after_seconds: 2.1,
      recoverable: true,
      message: "Provider throttled request.",
      seq: 77,
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(pushToast).toHaveBeenCalledWith(
      "模型请求受到速率限制 · 提供商：supertoken · 3 秒后重试 · Provider throttled request.",
      "warning",
      5000,
    );
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "provider",
        targetId: "rate-limit:conv-active:77",
        payload: expect.objectContaining({
          event: "rate_limit",
          conversation_id: "conv-active",
          provider: "supertoken",
          retry_after_seconds: 2.1,
          recoverable: true,
        }),
      }),
    ]);

    vi.clearAllMocks();
    useAppStore.setState({ inspectorEntries: [] });
    expect(handleRuntimeEvent({
      type: "rate_limit",
      conversation_id: "conv-other",
      provider: "other-provider",
      error_type: "busy",
    } as unknown as ServerEvent, "conv-other")).toBe(true);
    expect(pushToast).not.toHaveBeenCalled();
    expect(useAppStore.getState().inspectorEntries).toEqual([]);
  });

  it("hydrates replayed rate-limit diagnostics without repeating the transient toast", () => {
    expect(handleRuntimeEvent({
      type: "rate_limit",
      conversation_id: "conv-active",
      provider: "openai",
      error_type: "concurrency_limit",
      retry_at: Date.now() + 5_000,
      recoverable: true,
      seq: 91,
      replayed: true,
    } as unknown as ServerEvent, "conv-active")).toBe(true);

    expect(pushToast).not.toHaveBeenCalled();
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({ targetId: "rate-limit:conv-active:91" }),
    ]);
  });

  it("projects working and idle state into the owned conversation without contaminating the active one", () => {
    expect(handleRuntimeEvent({
      type: "session.state_changed",
      conversation_id: "conv-other",
      state: "working",
      reason: "agent_run_started",
    } as unknown as ServerEvent, "conv-other")).toBe(true);
    expect(useAppStore.getState().conversationStreaming).toMatchObject({
      "conv-active": false,
      "conv-other": true,
    });
    expect(useAppStore.getState().isStreaming).toBe(false);

    expect(handleRuntimeEvent({
      type: "session.state_changed",
      conversation_id: "conv-active",
      state: "working",
    } as unknown as ServerEvent, "conv-active")).toBe(true);
    expect(useAppStore.getState().isStreaming).toBe(true);

    expect(handleRuntimeEvent({
      type: "session.state_changed",
      conversation_id: "conv-other",
      state: "idle",
      reason: "completed",
    } as unknown as ServerEvent, "conv-other")).toBe(true);
    expect(useAppStore.getState().conversationStreaming["conv-other"]).toBe(false);
    expect(useAppStore.getState().isStreaming).toBe(true);
  });
});
