import { beforeEach, describe, expect, it, vi } from "vitest";
import { pushToast } from "../overlays/ToastContainer";
import type { ServerEvent } from "../protocol/events";
import { useAppStore } from "../stores";
import { handleControlPlaneProjectionEvent } from "./controlPlaneEvents";

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

const checkpoint = (overrides: Record<string, unknown> = {}) => ({
  id: "checkpoint-1",
  conversation_id: "conv-active",
  session_id: "session-1",
  tool_call_id: "tool-1",
  tool_name: "write_file",
  workspace_root: "C:\\repo",
  paths: ["src/app.ts"],
  created_at: "2026-08-15T01:02:03Z",
  metadata: { reason: "before_write" },
  ...overrides,
});

describe("handleControlPlaneProjectionEvent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-08-15T00:00:00Z", workspaceRoot: "C:\\repo" },
        { id: "conv-other", title: "Other", updatedAt: "2026-08-15T00:00:00Z", workspaceRoot: "C:\\other" },
      ],
      workingDirectory: "C:\\repo",
      conversationHydration: {},
      permissionRulesByConversation: {},
      checkpointsByConversation: {},
      runCheckpointsByConversation: {},
      checkpointResumeByConversation: {},
      guidelineReloadsByConversation: {},
      recentWorkspaces: [],
      inspectorEntries: [],
      inspectorFocus: null,
      requestGitChanges: vi.fn(),
    });
  });

  it("tracks hydration per conversation and exposes active state to the inspector", () => {
    expect(handleControlPlaneProjectionEvent({
      type: "conversation.hydration.updated",
      conversation_id: "conv-active",
      is_hydrating: true,
      timestamp: "2026-08-15T01:00:00Z",
    } as ServerEvent)).toBe(true);
    expect(handleControlPlaneProjectionEvent({
      type: "conversation.hydration.updated",
      conversation_id: "conv-other",
      is_hydrating: false,
    } as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationHydration["conv-active"]).toEqual({
      isHydrating: true,
      updatedAt: Date.parse("2026-08-15T01:00:00Z"),
    });
    expect(state.conversationHydration["conv-other"]?.isHydrating).toBe(false);
    expect(state.inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "session",
        targetId: "hydration:conv-active",
        payload: expect.objectContaining({ is_hydrating: true }),
      }),
    ]);
  });

  it("projects complete permission rules without leaking inactive or replayed updates into notifications", () => {
    const activeEvent = {
      type: "permission.rules.updated",
      session_id: "session-1",
      conversation_id: "conv-active",
      source: "websocket.command",
      rules: {
        mode: "confirm",
        context_source: "conversation.runtime",
        system_deny: [{ pattern: "run_command(rm:*)", source: "system.always_deny" }],
        session_deny: [{ pattern: "write_file(secrets/*)", source: "conversation.runtime" }],
        session_overrides: [{ pattern: "read_file(*)", level: "allow", source: "conversation.runtime" }],
        session_prompt_rules: [{
          tool: "run_command",
          rule_content: "prompt: npm test",
          behavior: "allow",
          destination: "session",
          source: "exit_plan_mode",
        }],
      },
    } as unknown as ServerEvent;

    expect(handleControlPlaneProjectionEvent(activeEvent)).toBe(true);
    expect(useAppStore.getState().permissionRulesByConversation["conv-active"]).toMatchObject({
      mode: "confirm",
      contextSource: "conversation.runtime",
      sessionDeny: [{ pattern: "write_file(secrets/*)", source: "conversation.runtime" }],
      sessionOverrides: [{ pattern: "read_file(*)", level: "allow" }],
    });
    expect(pushToast).toHaveBeenCalledWith(
      "权限规则已更新：会话拒绝 1 条，覆盖 1 条，系统拒绝 1 条。",
      "info",
      4200,
    );
    expect(useAppStore.getState().inspectorEntries.at(-1)).toMatchObject({
      targetKind: "permission",
      targetId: "permission-rules:conv-active",
    });

    vi.clearAllMocks();
    useAppStore.setState({ inspectorEntries: [] });
    expect(handleControlPlaneProjectionEvent({
      ...activeEvent,
      conversation_id: "conv-other",
    } as ServerEvent)).toBe(true);
    expect(useAppStore.getState().permissionRulesByConversation["conv-other"]).toBeDefined();
    expect(useAppStore.getState().inspectorEntries).toEqual([]);
    expect(pushToast).not.toHaveBeenCalled();

    expect(handleControlPlaneProjectionEvent({
      ...activeEvent,
      replayed: true,
    } as ServerEvent)).toBe(true);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("stores file checkpoints, rejects stale workspace ownership, and refreshes Git after rewind", () => {
    expect(handleControlPlaneProjectionEvent({
      type: "checkpoint.created",
      ...checkpoint(),
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().checkpointsByConversation["conv-active"]?.checkpoints[0]).toMatchObject({
      id: "checkpoint-1",
      toolCallId: "tool-1",
      paths: ["src/app.ts"],
    });
    expect(pushToast).not.toHaveBeenCalled();

    expect(handleControlPlaneProjectionEvent({
      type: "checkpoint.list",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      checkpoints: [checkpoint({ id: "checkpoint-2", created_at: "2026-08-15T02:00:00Z" })],
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().checkpointsByConversation["conv-active"]?.checkpoints.map((item) => item.id)).toEqual(["checkpoint-2"]);

    expect(handleControlPlaneProjectionEvent({
      type: "checkpoint.list",
      conversation_id: "conv-active",
      workspace_root: "C:\\stale",
      checkpoints: [checkpoint({ id: "stale", workspace_root: "C:\\stale" })],
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().checkpointsByConversation["conv-active"]?.checkpoints.map((item) => item.id)).toEqual(["checkpoint-2"]);

    const requestGitChanges = useAppStore.getState().requestGitChanges as ReturnType<typeof vi.fn>;
    expect(handleControlPlaneProjectionEvent({
      type: "checkpoint.rewound",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      checkpoint: checkpoint({ id: "checkpoint-2", created_at: "2026-08-15T02:00:00Z" }),
    } as unknown as ServerEvent)).toBe(true);
    expect(requestGitChanges).toHaveBeenCalledTimes(1);
    expect(pushToast).toHaveBeenCalledWith(
      "已回滚到检查点 checkpoint-2：src/app.ts",
      "success",
      5200,
    );
  });

  it("projects run checkpoint inventory and resume outcomes with replay-safe feedback", () => {
    expect(handleControlPlaneProjectionEvent({
      type: "checkpoint.run.list",
      session_id: "session-1",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      checkpoints: [{ run_id: "run-1", session_id: "session-1", conversation_id: "conv-active", iteration: 4, iterations: 4, stopped_reason: "timeout", timestamp: 10, created_at: 10 }],
      runs: [{ run_id: "run-1", status: "partial" }],
      subagents: [{ subagent_id: "subagent-1", status: "done" }],
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().runCheckpointsByConversation["conv-active"]).toMatchObject({
      sessionId: "session-1",
      checkpoints: [{ runId: "run-1", iteration: 4, stoppedReason: "timeout" }],
      runs: [{ run_id: "run-1", status: "partial" }],
    });

    expect(handleControlPlaneProjectionEvent({
      type: "checkpoint.run.resume",
      resumed: true,
      session_id: "session-1",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      run_id: "run-1",
      iteration: 4,
      stopped_reason: "timeout",
    } as ServerEvent)).toBe(true);
    expect(useAppStore.getState().checkpointResumeByConversation["conv-active"]).toMatchObject({
      resumed: true,
      runId: "run-1",
      iteration: 4,
    });
    expect(pushToast).toHaveBeenCalledWith("已从运行 run-1 的第 4 轮恢复。", "success", 5200);

    vi.clearAllMocks();
    expect(handleControlPlaneProjectionEvent({
      type: "checkpoint.run.resume",
      resumed: false,
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      message: "No incomplete run checkpoint found.",
      replayed: true,
    } as unknown as ServerEvent)).toBe(true);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("surfaces enriched guideline reloads and preserves useful recent-workspace metadata", () => {
    expect(handleControlPlaneProjectionEvent({
      type: "guidelines.updated",
      conversation_id: "conv-active",
      workspace_root: "C:\\repo",
      message: "Project guidelines have been updated",
      path: "AGENTS.md",
      cache_cleared: true,
      effective_from: "next_turn",
      source_kind: "direct",
    } as ServerEvent)).toBe(true);
    expect(useAppStore.getState().guidelineReloadsByConversation["conv-active"]).toMatchObject({
      path: "AGENTS.md",
      cacheCleared: true,
      effectiveFrom: "next_turn",
    });
    expect(pushToast).toHaveBeenCalledWith(
      "已重新加载 “AGENTS.md”，从下一次 Agent 回合开始生效。",
      "info",
      5200,
    );

    expect(handleControlPlaneProjectionEvent({
      type: "workspace.recent.list",
      projects: [
        { path: "C:\\older", name: "Older", project_type: "node", last_opened: 10 },
        { path: "C:\\newer", name: "Newer", project_type: "python", last_opened: 20 },
      ],
    } as ServerEvent)).toBe(true);
    expect(useAppStore.getState().recentWorkspaces).toEqual([
      { path: "C:\\newer", name: "Newer", projectType: "python", lastOpened: 20 },
      { path: "C:\\older", name: "Older", projectType: "node", lastOpened: 10 },
    ]);
    expect(useAppStore.getState().inspectorEntries).toContainEqual(expect.objectContaining({
      targetKind: "workspace",
      targetId: "recent-workspaces",
      payload: expect.objectContaining({ project_count: 2 }),
    }));
  });
});
