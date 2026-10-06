import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { registerWebSocketSender } from "../protocol/ws-outbox";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handlePeripheralEvent } from "./peripheralEvents";
import { handleSessionEvent } from "./sessionEvents";
import { focusInspectorEntry } from "./inspectorEntries";
import { projectAgentViews } from "../lib/agent-view-model";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { normalizeContentBlocks } from "./transcriptHydration";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { buildActivitySidebarState } from "../shell/activitySidebarState";
import type { ActivitySidebarStateInput } from "../shell/activitySidebarState";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const event = (value: object) => value as ServerEvent;
const activityInput = (patch: Partial<ActivitySidebarStateInput> = {}): ActivitySidebarStateInput => ({
  conversationId: "owner-a", messages: [], todos: [], plan: null, agentProgress: [], ...patch,
});

describe("runtime metadata reaches owner-scoped UI", () => {
  beforeEach(() => {
    useAppStore.setState({ conversationId: "owner-a", workingDirectory: "C:/a", conversations: [
      { id: "owner-a", title: "A", updatedAt: "", workspaceRoot: "C:/a" },
      { id: "owner-b", title: "B", updatedAt: "", workspaceRoot: "C:/b" },
    ], messages: [], conversationMessages: {}, conversationAgentStates: {}, subagents: [],
    inspectorEntries: [], inspectorFocus: null, terminalSessions: [], terminalSnapshots: {}, backgroundTasks: [],
    pendingConversationSwitchId: null });
    registerWebSocketSender(null);
  });

  it("keeps a completion in its real message position through replay and transcript hydration", () => {
    useAppStore.setState({ messages: [{ id: "assistant-a", role: "assistant", content: "", artifacts: [], timestamp: 1, isStreaming: true,
      blocks: [{ type: "tool_call", record: { id: "before", name: "run_command", args: { command: "git status" }, status: "success" } }],
    }] });
    const completion = normalizeInboundServerEvent({
      type: "agent.progress", conversation_id: "owner-a", message_id: "assistant-a",
      id: "subagent-completed:worker-a:1", stage: "status", phase: "subagent", status: "completed",
      visibility: "timeline", message: "Verify prediction 已完成", label: "Verify prediction",
      subagent_id: "worker-a", subagent_name: "Verify prediction", subagent_identity: "/root/verify_prediction",
      subagent_status: "done", timestamp: "2026-10-06T06:00:00Z",
    })!;
    expect(completion).not.toBeNull();
    handleRuntimeEvent(completion);
    handleRuntimeEvent({ ...completion, replayed: true });
    useAppStore.getState().appendToolCallBlock({ id: "after", name: "run_command", args: { command: "git diff" }, status: "success" }, "owner-a", "assistant-a");
    const live = useAppStore.getState().messages[0];
    expect(live.blocks?.filter((block) => block.type === "progress")).toHaveLength(1);
    const restored = { ...live, blocks: normalizeContentBlocks(live.blocks) };
    const cells = projectMessagesToTurns([restored], false)[0].committedCells;
    expect(cells.map((cell) => cell.kind)).toEqual(["exec", "collaboration", "exec"]);
    expect(cells[1]).toMatchObject({ action: "completed", status: "success",
      createdAt: Date.parse("2026-10-06T06:00:00Z"),
      entries: [{ agentId: "worker-a", agentLabel: "Verify prediction", agentIdentity: "/root/verify_prediction" }],
    });
  });

  it("routes a background completion to its owner without altering the active transcript", () => {
    useAppStore.setState({ messages: [{ id: "assistant-a", role: "assistant", content: "A", artifacts: [], timestamp: 1 }],
      conversationMessages: { "owner-b": [{ id: "assistant-b", role: "assistant", content: "", artifacts: [], timestamp: 1, blocks: [], isStreaming: true }] },
    });
    handleRuntimeEvent(event({ type: "agent.progress", conversation_id: "owner-b", message_id: "assistant-b",
      id: "subagent-completed:worker-b:2", stage: "status", phase: "subagent", status: "partial", message: "Worker B 已停止",
      subagent_id: "worker-b", subagent_name: "Worker B", subagent_status: "cancelled",
    }));
    expect(useAppStore.getState().messages[0].content).toBe("A");
    expect(useAppStore.getState().messages[0].blocks).toBeUndefined();
    const cells = projectMessagesToTurns(useAppStore.getState().conversationMessages["owner-b"], false)[0].committedCells;
    expect(cells[0]).toMatchObject({ kind: "collaboration", action: "completed", status: "cancelled" });
  });

  it("keeps a terminal subagent's pending cleanup actionable after live done", () => {
    handleRuntimeEvent(event({ type: "subagent.done", conversation_id: "owner-a", subagent_id: "child",
      status: "completed", summary: "result retained", record: { cleanup_pending: true, cleanup_reason: "runner did not exit" } }));
    const state = useAppStore.getState();
    expect(state.subagents[0]).toMatchObject({ status: "done", cleanupPending: true, cleanupReason: "runner did not exit" });
    expect(projectAgentViews(state.subagents)[0]).toMatchObject({ status: "attention", canStop: true, summary: "runner did not exit" });
    handleRuntimeEvent(event({ type: "subagent.done", conversation_id: "owner-a", subagent_id: "child", status: "completed",
      cleanup_pending: false, cleanup_reason: "" }));
    expect(useAppStore.getState().subagents[0]).toMatchObject({ cleanupPending: false, cleanupReason: "" });
  });

  it("retains cleanup ownership in a restored canonical snapshot", () => {
    const buffer = { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() };
    handleSessionEvent(event({ type: "conversation.switched", conversation_id: "owner-a", conversation: {
      id: "owner-a", title: "A", updated_at: "", transcript: [], context_snapshot: { ui_agent_state: {
        subagents: [{ id: "child", role: "explore", status: "done", cleanupPending: true, cleanupReason: "runner alive" }],
      } },
    } }), { textStreamBuffer: buffer, thinkingStreamBuffer: buffer });
    expect(useAppStore.getState().subagents[0]).toMatchObject({ cleanupPending: true, cleanupReason: "runner alive" });
  });

  it("deduplicates Inspector by conversation, kind and id", () => {
    const state = useAppStore.getState();
    for (const owner of ["owner-a", "owner-b"]) state.addInspectorEntry({ targetKind: "provider", targetId: "trace",
      payload: { conversation_id: owner }, timestamp: 1 });
    state.addInspectorEntry({ targetKind: "tool_call", targetId: "trace", payload: { conversation_id: "owner-a" }, timestamp: 1 });
    expect(useAppStore.getState().inspectorEntries).toHaveLength(3);
  });

  it("requests deferred diagnostics from the recorded owner after switching", () => {
    const commands: unknown[] = [];
    registerWebSocketSender((command) => { commands.push(command); return true; });
    useAppStore.setState({ conversationId: "owner-b", workingDirectory: "C:/b" });
    focusInspectorEntry({ targetKind: "provider", targetId: "trace", payload: { conversation_id: "owner-a", diagnostics_deferred: true }, timestamp: 1 });
    expect(commands[0]).toMatchObject({ conversation_id: "owner-a", workspace_root: "C:/a" });
    expect(useAppStore.getState().inspectorFocus).toMatchObject({ conversationId: "owner-a", kind: "provider", id: "trace" });
  });

  it("preserves signal and unknown exit code at the terminal event boundary", () => {
    useAppStore.getState().upsertTerminalSession({ id: "term", conversationId: "owner-a", shell: "pwsh", cwd: "C:/a", status: "running" });
    handlePeripheralEvent(event({ type: "terminal.exit", conversation_id: "owner-a", session_id: "term", exit_code: null, exit_signal: "SIGTERM" }));
    expect(useAppStore.getState().terminalSessions[0]).toMatchObject({ status: "exited", exitCode: null, exitSignal: "SIGTERM" });
  });

  it("shows unknown terminal outcome and omits another conversation's terminal", () => {
    const runs = buildActivitySidebarState(activityInput({ terminalSessions: [
      { id: "unknown", conversationId: "owner-a", shell: "pwsh", cwd: "C:/a", status: "exited" },
      { id: "other", conversationId: "owner-b", shell: "pwsh", cwd: "C:/b", status: "running" },
    ] })).runs;
    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({ terminalId: "unknown", status: "unknown", attention: true });
  });

  it("uses retained exit metadata for a terminal snapshot without a session row", () => {
    const runs = buildActivitySidebarState(activityInput({ activeTerminalSessionId: "term", terminalSnapshots: { term: {
      id: "term", conversationId: "owner-a", shell: "pwsh", cwd: "C:/a", status: "exited", output: "", capturedAt: 1,
      exitCode: 7,
    } } } as Partial<ActivitySidebarStateInput>)).runs;
    expect(runs[0].status).toBe("failed");
  });

  it("retains pending background cleanup and its reason through display and history trimming", () => {
    handlePeripheralEvent(event({ type: "background.completed", conversation_id: "owner-a", command_id: "pending", command: "worker",
      status: "cancelled", cleanup_pending: true, cleanup_reason: "child still alive", replayed: true }));
    for (let i = 0; i < 35; i++) useAppStore.getState().addBackgroundTask({ id: `done-${i}`, command: "done", status: "completed", timestamp: i, conversationId: "owner-a" });
    const task = useAppStore.getState().backgroundTasks.find((item) => item.id === "pending");
    expect(task).toMatchObject({ cleanupPending: true, cleanupReason: "child still alive", terminalStatus: "cancelled" });
    const runs = buildActivitySidebarState(activityInput({ backgroundTasks: useAppStore.getState().backgroundTasks })).runs;
    expect(runs[0]).toMatchObject({ id: "background:pending", status: "cleanup_pending", detail: "清理未完成 · child still alive", attention: true });
  });

  it("does not let late lifecycle metadata replace a terminal result while allowing transcript updates", () => {
    const state = useAppStore.getState();
    state.addSubagent({ id: "child", role: "explore", status: "done", summary: "Final result", cleanupPending: true });
    state.updateSubagent("child", { status: "running", summary: "Older activity", cleanupPending: false });
    expect(useAppStore.getState().subagents[0]).toMatchObject({ status: "done", summary: "Final result", cleanupPending: true });
    state.updateSubagent("child", { transcriptSeq: 4, transcriptMessages: [], cleanupPending: false });
    expect(useAppStore.getState().subagents[0]).toMatchObject({ transcriptSeq: 4, cleanupPending: false });
  });
});
