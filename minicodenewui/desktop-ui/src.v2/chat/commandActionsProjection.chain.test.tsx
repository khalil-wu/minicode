/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import capturedPayload from "../../test/fixtures/real-command-actions-wire-history.json";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import type { ToolCallEvent, ToolResultEvent } from "../protocol/events";
import type { SubagentTranscriptEventsDelta } from "../protocol/streaming-types";
import type { BackendTranscriptMessage } from "./transcriptHydration";
import { reduceToolCallResult, reduceToolCallStart, type ToolCallRecord } from "../lib/tool-call-reducer";
import { activityKindFromToolRecord } from "../lib/turn-projection";
import { mergeResumeToolCalls } from "../stores/shared-helpers";
import { hydrateMessages } from "./transcriptHydration";
import { applySubagentTranscriptEvents } from "./subagentTranscript";
import { createRecentTurnProjectionCache, projectMessagesToTurns, projectRecentMessagesToTurns } from "./chatSurfaceState";
import { inheritMessageTopology, recordStreamingToolUpdate } from "../lib/message-changes";
import type { ChatMessage } from "../stores/types";
import { ExecCell } from "./cells/ExecCell";
import { ActivityCell } from "./cells/ActivityCell";
import { AgentTimeline } from "../agent-loop/components/AgentTimeline";
import { useActivitySummary } from "../agent-loop/components/useActivitySummary";
import { useProcessDisclosure } from "../agent-loop/components/useProcessDisclosure";
import { useAppStore } from "../stores";
import { CollaborationCell } from "./cells/CollaborationCell";
import { CLIENT_COMMAND_TYPES } from "../protocol/events";

const previousReducedMotion = useAppStore.getState().reducedMotion;
const previousAnimate = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "animate");
beforeEach(() => {
  useAppStore.setState({ reducedMotion: true });
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("prefers-reduced-motion"), addEventListener() {}, removeEventListener() {} }));
});
afterEach(() => {
  cleanup(); vi.useRealTimers(); vi.unstubAllGlobals();
  if (previousAnimate) Object.defineProperty(HTMLElement.prototype, "animate", previousAnimate);
  else delete (HTMLElement.prototype as Partial<HTMLElement>).animate;
  useAppStore.setState({ reducedMotion: previousReducedMotion });
});

const command = "Get-Content -LiteralPath src/app.ts";
const actions: NonNullable<ToolCallRecord["commandActions"]> = [{ type: "read", command, name: "app.ts", path: "C:/project/src/app.ts" }];
const call = { type: "tool_call", id: "read-command", name: "run_command", args: { command }, command_actions: actions, status: "running", activity_kind: "commandExecution", result_kind: "command" };
const result = { type: "tool_result", id: "read-command", summary: "const actual = 42;", command_actions: actions, status: "success", content_preview: "const actual = 42;", activity_kind: "commandExecution", result_kind: "command" };

describe("backend parsed command actions across live, history, resume and child projection", () => {
  it("projects each typed shell action with stable IDs and one shared execution receipt across streamed updates", () => {
    const originalCommand = "cat source.ts && rg value src";
    const commandActions: NonNullable<ToolCallRecord["commandActions"]> = [
      { type: "read", command: "cat source.ts", name: "source.ts", path: "/project/source.ts" },
      { type: "search", command: "rg value src", query: "value", path: "src" },
    ];
    const record: ToolCallRecord = { id: "compound", name: "run_command", args: { command: originalCommand }, commandActions, status: "running", startedAt: 1, stdoutPreview: "shared initial stdout", stderrPreview: "" };
    const message: ChatMessage = { id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1, isStreaming: true, blocks: [{ type: "tool_call", record }] };
    const cache = createRecentTurnProjectionCache();
    const messages = [message];
    const first = projectRecentMessagesToTurns(messages, true, 1, cache, "compound").turns[0].committedCells;
    expect(first).toHaveLength(2);
    expect(first.map(cell => cell.id)).toEqual(["compound:0", "compound:1"]);
    expect(first).toEqual([expect.objectContaining({ kind: "exec", command: "cat source.ts", originalCommand, commandExecutionItemId: record.id }),
      expect.objectContaining({ kind: "exec", command: "rg value src", originalCommand, commandExecutionItemId: record.id })]);
    const renderCells = (cells: typeof first) => <>{cells.map(cell => cell.kind === "exec" && <ExecCell key={cell.id} cell={cell} />)}</>;
    const view = render(renderCells(first));
    fireEvent.click(screen.getAllByRole("button", { name: /展开命令详情/ })[0]);
    expect(screen.getByText(`$ ${originalCommand}`)).toBeTruthy();
    const completed: ChatMessage = { ...message, blocks: [{ type: "tool_call", record: { ...record, status: "failed", exitCode: 7, stdoutPreview: "shared final stdout", stderrPreview: "actual command error", durationMs: 2700 } }] };
    recordStreamingToolUpdate(message, completed, 0);
    const nextMessages = [completed];
    inheritMessageTopology(messages, nextMessages);
    const second = projectRecentMessagesToTurns(nextMessages, true, 1, cache, "compound").turns[0].committedCells;
    expect(second).toHaveLength(2);
    expect(second.map(cell => cell.id)).toEqual(first.map(cell => cell.id));
    for (const cell of second) expect(cell).toMatchObject({ commandExecutionItemId: record.id, stdoutFull: "shared final stdout", stderrFull: "actual command error", exitCode: 7, durationMs: 2700 });
    view.rerender(renderCells(second));
    expect(view.container.querySelector('.exec-cell[data-command-execution-id=compound]')?.getAttribute("data-expanded")).toBe("true");
    expect(screen.getByText(`$ ${originalCommand}`)).toBeTruthy();
    expect(screen.getByText("actual command error")).toBeTruthy();
    expect(record.args.command).toBe(originalCommand);
    expect(record.commandActions).toBe(commandActions);
  });
  it("projects real backend read and exit 7 wire receipts, main history, reopened child journal and incremental child events identically", () => {
    // Captured from the backend's real tools; the independent model/WS tests
    // do not synthesize these result summaries or command-action fields.
    const captured = capturedPayload as unknown as {
      wire: Array<ToolCallEvent | ToolResultEvent>; main_blocks: unknown[]; child_messages: BackendTranscriptMessage[];
      child_incremental: SubagentTranscriptEventsDelta["events"];
    };
    let records = new Map<string, ToolCallRecord>();
    for (const wire of captured.wire) {
      const event = normalizeInboundServerEvent(wire)!;
      expect(event).toBeTruthy();
      records = event.type === "tool_call" ? reduceToolCallStart(records, event) : reduceToolCallResult(records, event as ToolResultEvent);
    }
    const live = [{ id: "live", role: "assistant" as const, content: "", artifacts: [], timestamp: 1, blocks: [...records.values()].map(record => ({ type: "tool_call" as const, record })) }];
    const main = hydrateMessages([{ id: "history", role: "assistant", content: "", timestamp: 1, blocks: captured.main_blocks }]);
    const child = hydrateMessages(captured.child_messages);
    const priorChild = child.filter(message => message.role === "assistant").map(message => ({ ...message, blocks: [], content: "", isStreaming: true }));
    const incremental = applySubagentTranscriptEvents(priorChild, { kind: "events", base_seq: 0, seq: 5, events: captured.child_incremental })!;
    expect(incremental).toBeTruthy();
    for (const messages of [live, main, child, incremental]) {
      const cells = projectMessagesToTurns(messages, false).flatMap(turn => turn.committedCells).filter(cell => cell.kind === "exec");
      expect(cells).toHaveLength(2);
      expect(cells[0]).toMatchObject({ kind: "exec", commandActions: [expect.objectContaining({ type: "read", name: "source.txt" })], status: "success" });
      expect(cells[1]).toMatchObject({ kind: "exec", commandActions: [expect.objectContaining({ type: "unknown" })], status: "failed", exitCode: 7 });
      const view = render(<>{cells.map(cell => <ExecCell key={cell.id} cell={cell} />)}</>);
      expect(screen.getByText("source.txt")).toBeTruthy();
      expect(view.container.querySelector(".exec-cell[data-status=failed] .exec-cell-header-button")?.textContent).not.toContain("失败");
      for (const button of screen.getAllByRole("button", { name: /展开命令详情/ })) fireEvent.click(button);
      expect(view.container.textContent).toContain("real file evidence");
      expect(view.container.textContent).toContain("actual exit evidence");
      expect(view.container.textContent).toContain("exit 7");
      cleanup();
    }
  });
  it("keeps the canonical read action and original shell evidence through every transcript entry point", () => {
    const incomingCall = normalizeInboundServerEvent(call) as ToolCallEvent;
    const incomingResult = normalizeInboundServerEvent(result) as ToolResultEvent;
    expect(incomingCall.command_actions).toEqual(actions);
    expect(incomingResult.command_actions).toEqual(actions);
    const live = reduceToolCallResult(reduceToolCallStart(new Map(), incomingCall, 1000), incomingResult, 2000).get(call.id)!;
    const historicalWire = { type: "conversation.switched", conversation_id: "owner", conversation: { id: "owner", transcript: [{ id: "answer", role: "assistant", timestamp: 1000,
      blocks: [{ type: "tool_call", record: { ...live, commandActions: undefined, command_actions: actions } }] }] } };
    expect(normalizeInboundServerEvent(JSON.parse(JSON.stringify(historicalWire)))).toBeTruthy();
    const history = hydrateMessages(historicalWire.conversation.transcript)[0].blocks![0];
    const resumeWire = { type: "stream_resume", conversation_id: "owner", message_id: "answer", tool_calls_pending: [{ id: call.id, name: call.name, args: call.args, status: "running", command_actions: actions }] };
    expect(normalizeInboundServerEvent(resumeWire)).toBeTruthy();
    const resumed = mergeResumeToolCalls([], resumeWire.tool_calls_pending)[0];
    const child = applySubagentTranscriptEvents([], { kind: "events", base_seq: 0, seq: 3, events: [
      { seq: 1, event_id: "child-start", event_type: "system", ts_ms: 1000, payload: { lifecycle: "turn_started", turn_id: "child-turn" } },
      { seq: 2, event_id: "child-tool", event_type: "tool_use", ts_ms: 1100, payload: { tool_call: { ...call, started_at: 1100 } } },
      { seq: 3, event_id: "child-result", event_type: "tool_result", ts_ms: 2000, payload: { tool_call_id: call.id, record: { ...live, commandActions: undefined, command_actions: actions } } },
    ] })![0].blocks![0];
    for (const entry of [{ type: "tool_call", record: live } as const, history, resumed, child]) {
      expect(entry.type).toBe("tool_call");
      if (entry.type !== "tool_call") throw new Error("Missing canonical tool record");
      expect(entry.record.commandActions).toEqual(actions);
      expect(activityKindFromToolRecord(entry.record)).toBe("fileRead");
      const cell = projectMessagesToTurns([{ id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1000, blocks: [entry] }], false)[0].committedCells[0];
      expect(cell).toMatchObject({ kind: "exec", command, commandActions: actions });
      if (cell.kind !== "exec") throw new Error("Shell evidence changed cell identity");
      const view = render(<ExecCell cell={cell} />);
      expect(screen.getByText("app.ts")).toBeTruthy();
      expect(view.container.querySelector(".lucide-book-open-text")).toBeTruthy();
      expect(screen.queryByText(command)).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: /展开命令详情/ }));
      expect(screen.getByText(`$ ${command}`)).toBeTruthy();
      if (entry !== resumed) expect(screen.getByText("const actual = 42;")).toBeTruthy();
      cleanup();
    }
  });

  it.each([
    [{ type: "search", command: "rg process src", query: "process", path: "src" }, "workspaceSearch", "process · src"],
    [{ type: "listFiles", command: "ls src", path: "src" }, "workspaceList", "src"],
    [{ type: "unknown", command: "npm test" }, "commandExecution", "npm test"],
  ] as const)("renders a typed %s action without reparsing shell text", (action, kind, target) => {
    const record: ToolCallRecord = { id: "action", name: "run_command", args: { command: action.command }, commandActions: [action], status: "running", startedAt: 1000 };
    expect(activityKindFromToolRecord(record)).toBe(kind);
    const cell = projectMessagesToTurns([{ id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1000, blocks: [{ type: "tool_call", record }] }], true)[0].committedCells[0];
    if (cell.kind !== "exec") throw new Error("Missing command cell");
    const view = render(<ExecCell cell={cell} />);
    expect(screen.getByText(target)).toBeTruthy();
    expect(view.container.querySelector(".exec-cell")?.getAttribute("data-activity-kind")).toBe(kind);
  });

  it.each(["failed", "blocked", "timeout", "partial"] as const)("keeps %s tool diagnostics folded without terminal status decorations", (status) => {
    const record: ToolCallRecord = { id: status, name: "read_file", args: { path: "missing.ts" }, status, startedAt: 1, userSummary: "Cannot read missing.ts", outputPreview: "FileNotFoundError: missing.ts" };
    const view = render(<ActivityCell cell={{ kind: "activity", id: status, activityKind: "fileRead", title: "Read", status: status === "partial" ? "partial" : "failed", collapsed: true, startedAt: 1, toolCallRecords: [record] }} />);
    expect(view.container.querySelector(".activity-cell-main-button")?.textContent).not.toMatch(/失败|超时|已阻止|部分完成/);
    expect(screen.queryByText("FileNotFoundError: missing.ts")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
    expect(view.container.textContent).toContain("FileNotFoundError: missing.ts");
    expect(record.status).toBe(status);
  });
});

describe("source-backed Codex activity disclosure transitions", () => {
  it("retains independent VM diagnostics behind a neutral operation title", () => {
    const raw = JSON.stringify({ cell_id: "real-cell", status: "failed", error: "TypeError: real diagnostic" });
    const record: ToolCallRecord = { id: "vm-error", name: "tool_exec", args: { code: "throw new TypeError()" }, status: "failed", startedAt: 1, outputPreview: raw };
    const cell = projectMessagesToTurns([{ id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1, blocks: [{ type: "tool_call", record }] }], false)[0].committedCells[0];
    expect(cell).toMatchObject({ kind: "activity", title: "运行", status: "failed", outputText: "TypeError: real diagnostic" });
    if (cell.kind !== "activity") throw new Error("Missing actual VM evidence");
    const view = render(<ActivityCell cell={cell} />);
    expect(view.container.querySelector(".activity-cell-main-button")?.textContent).toBe("运行");
    expect(screen.queryByText("TypeError: real diagnostic")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "展开活动详情" }));
    expect(screen.getByText("TypeError: real diagnostic")).toBeTruthy();
    expect(screen.getByText("TypeError: real diagnostic").tagName).toBe("PRE");
    expect(record.outputPreview).toBe(raw);
  });

  it("stops animating a past group when a later narration owns the current activity", () => {
    const cells = ["one", "two"].map(id => ({ kind: "exec" as const, id, command: `run ${id}`, status: "running" as const, stdoutPreview: [], stderrPreview: [], collapsed: true, createdAt: 1, segment: 1, segmentClosed: true }));
    const view = render(<AgentTimeline cells={[...cells, { kind: "thinking", id: "commentary", source: "commentary", content: "接下来的实际步骤", isStreaming: true, createdAt: 2, segment: 2 }]} isRunning renderCell={({ cell, key }) => <div key={key}>{cell.id}</div>} />);
    const header = screen.getByRole("button", { name: "运行了命令" });
    expect(header.parentElement?.getAttribute("data-processing")).toBe("false");
    expect(view.container.querySelector(".agent-loop-timeline-group-live-title")).toBeNull();
    expect(screen.getByText("commentary")).toBeTruthy();
    expect(cells[0].status).toBe("running");
  });

  it("admits native followup commands and shows the existing real identity without another startup or dispatch prompt", () => {
    expect(CLIENT_COMMAND_TYPES.has("followup_task")).toBe(true);
    useAppStore.setState({ conversationId: "followup-owner", subagents: [{ id: "worker-real", agentPath: "/root/worker_real", displayName: "真实工作者", role: "subagent", status: "running", startedAt: 1 }] });
    const record: ToolCallRecord = { id: "followup", name: "followup_task", args: { target: "untrusted alias", message: "内部委派正文" }, recipientId: "worker-real", status: "success", startedAt: 2 };
    const cell = projectMessagesToTurns([{ id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1, blocks: [{ type: "tool_call", record }] }], false)[0].committedCells[0];
    expect(cell).toMatchObject({ kind: "collaboration", action: "sent_message", entries: [{ agentId: "worker-real" }] });
    if (cell.kind !== "collaboration") throw new Error("Followup was projected as a new spawn");
    const view = render(<CollaborationCell cell={cell} conversationId="followup-owner" />);
    expect(screen.getByRole("button", { name: "打开子智能体：真实工作者" }).textContent).toBe("真实工作者");
    expect(view.container.textContent).not.toContain("内部委派正文");
    expect(view.container.textContent).not.toContain("开始工作");
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：真实工作者" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("worker-real");
  });

  it("uses the backend's broadcast receiver identities without inventing an all-agents identity", () => {
    useAppStore.setState({ conversationId: "broadcast-owner", subagents: [
      { id: "worker-real", agentPath: "/root/worker_real", displayName: "真实工作者", role: "subagent", status: "running" },
      { id: "worker-second", agentPath: "/root/worker_second", displayName: "第二位工作者", role: "subagent", status: "completed" },
    ] });
    const record: ToolCallRecord = { id: "broadcast", name: "send_message", args: { recipient: "*", message: "内部广播正文" }, recipientIds: ["worker-real", "worker-second"], status: "success", startedAt: 2 };
    const cell = projectMessagesToTurns([{ id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1, blocks: [{ type: "tool_call", record }] }], false)[0].committedCells[0];
    expect(cell).toMatchObject({ kind: "collaboration", entries: [{ agentId: "worker-real" }, { agentId: "worker-second" }] });
    if (cell.kind !== "collaboration") throw new Error("Missing actual receiver identities");
    const view = render(<CollaborationCell cell={cell} conversationId="broadcast-owner" />);
    expect(screen.getByRole("button", { name: "打开子智能体：真实工作者" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "打开子智能体：第二位工作者" })).toBeTruthy();
    expect(view.container.querySelectorAll(".mc-agent-avatar")).toHaveLength(2);
    expect(view.container.textContent).not.toMatch(/所有智能体|内部广播正文|开始工作/);
  });

  it("keeps a real parent-mailbox message separate from ordinary child identities", () => {
    const record: ToolCallRecord = { id: "parent-send", name: "send_message", args: { target: "/root", message: "actual parent message" }, recipientIds: ["parent"], status: "success", startedAt: 1 };
    const cell = projectMessagesToTurns([{ id: "answer", role: "assistant", content: "", artifacts: [], timestamp: 1, blocks: [{ type: "tool_call", record }] }], false)[0].committedCells[0];
    expect(cell).toMatchObject({ kind: "collaboration", entries: [{ agentId: "parent" }] });
    if (cell.kind !== "collaboration") throw new Error("Missing parent mailbox fact");
    const view = render(<CollaborationCell cell={cell} conversationId="parent-owner" isTranscriptMode />);
    expect(screen.getByText("已向父代理发送消息")).toBeTruthy();
    expect(view.container.querySelector(".mc-agent-avatar")).toBeNull();
    expect(view.container.textContent).not.toContain("actual parent message");
    expect(record.args.target).toBe("/root");
  });
  it("keeps an active title for one second, then immediately switches to its terminal summary", () => {
    vi.useFakeTimers();
    const view = renderHook(({ title, key, active }) => useActivitySummary(title, key, active), { initialProps: { title: "正在读取 app.ts", key: "read", active: true } });
    view.rerender({ title: "正在运行 npm test", key: "test", active: true });
    expect(view.result.current.title).toBe("正在读取 app.ts");
    act(() => vi.advanceTimersByTime(999));
    expect(view.result.current.title).toBe("正在读取 app.ts");
    act(() => vi.advanceTimersByTime(1));
    expect(view.result.current.title).toBe("正在运行 npm test");
    view.rerender({ title: "已读取文件运行了命令", key: "summary", active: false });
    expect(view.result.current.title).toBe("已读取文件运行了命令");
  });

  it("retains the body during the real 300 ms height and opacity closing transition", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    useAppStore.setState({ reducedMotion: false });
    const motions: { onfinish: null | (() => void); cancel: ReturnType<typeof vi.fn> }[] = [];
    const animate = vi.fn((_frames: Keyframe[], _options: KeyframeAnimationOptions) => { const motion = { onfinish: null as null | (() => void), cancel: vi.fn() }; motions.push(motion); return motion; });
    Object.defineProperty(HTMLElement.prototype, "animate", { configurable: true, value: animate });
    function Disclosure({ expanded }: { expanded: boolean }) {
      const state = useProcessDisclosure(expanded);
      return state.mounted ? <div ref={state.element}>real tool evidence</div> : null;
    }
    const view = render(<Disclosure expanded={false} />);
    view.rerender(<Disclosure expanded />);
    expect(screen.getByText("real tool evidence")).toBeTruthy();
    expect(animate.mock.calls[0][1]).toMatchObject({ duration: 300, easing: "cubic-bezier(0.19, 1, 0.22, 1)" });
    act(() => motions[0].onfinish!());
    view.rerender(<Disclosure expanded={false} />);
    expect(screen.getByText("real tool evidence")).toBeTruthy();
    act(() => motions[1].onfinish!());
    expect(screen.queryByText("real tool evidence")).toBeNull();
  });

  it("keeps completed read, search and directory records in event order under one exploration summary", () => {
    const cells = ["workspaceList", "workspaceSearch", "fileRead"].map((activityKind, index) => ({ kind: "activity" as const, id: String(index), activityKind: activityKind as "workspaceList" | "workspaceSearch" | "fileRead", title: "real action", status: "done" as const, collapsed: true, startedAt: index, segment: 1 }));
    const view = render(<AgentTimeline cells={cells} renderCell={({ cell, key }) => <div key={key}>{cell.id}</div>} />);
    fireEvent.click(screen.getByRole("button", { name: "已读取文件" }));
    expect([...view.container.querySelectorAll(".agent-loop-timeline-group-items > div")].map(element => element.textContent)).toEqual(["0", "1", "2"]);
  });

  it("keeps a single active command as an activity group and turns its completed row into standalone evidence", () => {
    const cell = { kind: "exec" as const, id: "single", command: "npm test", status: "running" as const, stdoutPreview: [], stderrPreview: [], collapsed: true, createdAt: 1, segment: 1 };
    const view = render(<AgentTimeline cells={[cell]} isRunning renderCell={({ cell, key }) => <div key={key}>{cell.id}</div>} />);
    const header = screen.getByRole("button", { name: "正在运行 npm test" });
    expect(header.parentElement?.getAttribute("data-processing")).toBe("true");
    expect(screen.queryByText("single")).toBeNull();
    fireEvent.click(header);
    expect(screen.getByText("single")).toBeTruthy();
    view.rerender(<AgentTimeline cells={[{ ...cell, status: "success" }]} renderCell={({ cell, key }) => <div key={key}>{cell.id}</div>} />);
    expect(screen.getByText("single")).toBeTruthy();
    expect(view.container.querySelector(".agent-loop-timeline-group-work")).toBeNull();
  });
});
