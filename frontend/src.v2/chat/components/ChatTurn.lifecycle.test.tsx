/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatTurn } from "./ChatTurn";
import { handleChatStreamEvent } from "../chatStreamEvents";
import { projectMessagesToTurns } from "../chatSurfaceState";
import { hydrateMessages } from "../transcriptHydration";
import { releaseConversationReadingState } from "../transcriptReadingState";
import { TranscriptSearchContext } from "../TranscriptSearchContext";
import { useAppStore } from "../../stores";
import type { ServerEvent } from "../../protocol/events";
import type { StreamBuffer } from "../../lib/stream-buffer";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable:true, value:() => ({matches:false,addEventListener(){},removeEventListener(){}}) }));
vi.mock("../../protocol/ws-outbox", () => ({ sendClientCommand:vi.fn() }));
vi.mock("../../overlays/ToastContainer", () => ({ pushToast:vi.fn() }));

const buffer = (): StreamBuffer => ({push:vi.fn(),flush:vi.fn(),destroy:vi.fn()});
const handlers = {textStreamBuffer:buffer(),thinkingStreamBuffer:buffer()};
const handle = (event: Record<string, unknown>) => handleChatStreamEvent(event as ServerEvent,"lifecycle",handlers);
const turn = () => projectMessagesToTurns(useAppStore.getState().messages,useAppStore.getState().isStreaming)[0];
const complete = () => handle({type:"item.completed",conversation_id:"lifecycle",message_id:"assistant",turn_id:"actual-turn",
  item:{id:"model-final",type:"agent_message",text:"完成验证。",source:"model_final",status:"completed"},finish_reason:"stop"});
const done = () => handle({type:"done",conversation_id:"lifecycle",message_id:"assistant",turn_id:"actual-turn",status:"completed",usage:{}});
const replaceTool = (record: ToolCallRecord) => useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant"
  ? { ...message, blocks: message.blocks?.map((block) => block.type === "tool_call" ? { ...block, record } : block) } : message) }));
const rawTool = (kind: "exec" | "mcp"): ToolCallRecord => ({ id:"read", name:kind === "exec" ? "run_command" : "catalog_lookup",
  args:kind === "exec" ? { command:"printf 'first'" } : { query:"actual catalog" },
  activityKind:kind === "exec" ? "commandExecution" : "mcpToolCall", resultKind:kind === "exec" ? "command" : "mcp",
  status:"success", startedAt:3, finishedAt:4, stdoutPreview:kind === "exec" ? "Protected raw output" : undefined,
  outputPreview:kind === "mcp" ? "Protected MCP output" : undefined, exitCode:kind === "exec" ? 0 : undefined });

beforeEach(() => {
  releaseConversationReadingState("lifecycle");
  useAppStore.setState({conversationId:"lifecycle",pendingConversationSwitchId:null,isStreaming:true,
  conversationStreaming:{lifecycle:true},conversationMessages:{},sideChats:{},draft:"保留的草稿",viewMode:"normal",messages:[
    {id:"user",role:"user",content:"父任务指派：检查渲染",timestamp:1},
    {id:"assistant",role:"assistant",content:"",timestamp:2,isStreaming:true,turnId:"actual-turn",blocks:[
      {type:"text",itemId:"commentary",content:"我会先查询来源并核对日期。",source:"commentary",status:"completed",isStreaming:false},
      {type:"tool_call",record:{id:"read",name:"read_file",args:{file_path:"README.md"},status:"success",startedAt:3,finishedAt:4}},
    ]},
  ]});
});
afterEach(cleanup);

describe("real item/terminal disclosure and child roles", () => {
  it("folds work in the first committed paint containing the completed answer", () => {
    const committedDisclosure: string[] = [];
    const captureCommit = () => {
      if (screen.queryByText("完成验证。")) {
        committedDisclosure.push(screen.getByLabelText("Agent 处理进度").getAttribute("data-collapsed")!);
      }
    };
    const {rerender}=render(<Profiler id="actual-disclosure-commit" onRender={captureCommit}><ChatTurn turn={turn()} /></Profiler>);
    complete();
    rerender(<Profiler id="actual-disclosure-commit" onRender={captureCommit}><ChatTurn turn={turn()} /></Profiler>);
    expect(committedDisclosure.length).toBeGreaterThan(0);
    expect(committedDisclosure.every((value) => value === "true")).toBe(true);
    done();
    rerender(<Profiler id="actual-disclosure-commit" onRender={captureCommit}><ChatTurn turn={turn()} /></Profiler>);
    expect(screen.getByLabelText("Agent 处理进度").getAttribute("data-collapsed")).toBe("true");
  });
  it.each([false,true])("collapses work on committed model_final before done in transcript=%s and stays collapsed after done", (child) => {
    const {container,rerender}=render(<ChatTurn turn={turn()} isTranscriptMode={child} />);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    complete();
    expect(useAppStore.getState().isStreaming).toBe(true);
    rerender(<ChatTurn turn={turn()} isTranscriptMode={child} />);
    expect(screen.getByText("完成验证。")).toBeTruthy();
    expect(screen.getByRole("button",{name:"展开处理步骤"}).getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("我会先查询来源并核对日期。")).toBeNull();
    expect(container.querySelector('.agent-loop-process-summary-wrap')?.getAttribute("data-position")).toBe("top");
    done();rerender(<ChatTurn turn={turn()} isTranscriptMode={child} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"}).getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(screen.getByRole("button",{name:"展开处理步骤"}));
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("保留的草稿");
  });

  it("folds after ordinary scrolling and preserves an explicit expansion across terminal delivery", () => {
    const {rerender}=render(<ChatTurn turn={turn()} />);
    fireEvent.wheel(screen.getByLabelText("Agent 处理进度"),{deltaY:-120});
    complete();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"展开处理步骤"}));
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    done();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:"收起处理步骤"})).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("保留的草稿");
  });

  it.each([false,true])("folds the parent after reading tool details and restores the child choice on reopen, transcript=%s", (child) => {
    useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant" ? { ...message,
      blocks: message.blocks?.map((block) => block.type === "tool_call" ? { ...block, record: { ...block.record, outputPreview: "Actual file contents" } } : block) } : message) }));
    const {rerender}=render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    fireEvent.click(screen.getByRole("button",{name:"展开活动详情"}));
    expect(screen.getByText("Actual file contents")).toBeTruthy();
    complete();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
    expect(screen.queryByText("我会先查询来源并核对日期。")).toBeNull();
    expect(screen.queryByText("Actual file contents")).toBeNull();
    done();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"展开处理步骤"}));
    expect(screen.getByText("Actual file contents")).toBeTruthy();
  });

  it.each([false,true])("keeps a group's own reading choice without pinning the parent open, transcript=%s", (child) => {
    useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant" ? { ...message,
      blocks: [...message.blocks!, { type: "tool_call", record: { id: "read-two", name: "read_file", args: { file_path: "second.md" },
        status: "success", startedAt: 4, finishedAt: 5, outputPreview: "Second file contents" } }] } : message) }));
    const {rerender}=render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    fireEvent.click(screen.getByRole("button", { name: "已读取文件" }));
    expect(screen.getByRole("button", { name: "已读取文件" }).getAttribute("aria-expanded")).toBe("true");
    complete();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name: "展开处理步骤" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "已读取文件" })).toBeNull();
    done();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    fireEvent.click(screen.getByRole("button", { name: "展开处理步骤" }));
    expect(screen.getByRole("button", { name: "已读取文件" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("second.md")).toBeTruthy();
  });

  it.each([
    ["exec",false,false], ["exec",false,true], ["exec",true,false], ["exec",true,true],
    ["mcp",false,false], ["mcp",false,true], ["mcp",true,false], ["mcp",true,true],
  ] as const)("protects explicit raw %s reading only on open, transcript=%s closeDetail=%s", (kind, child, closeDetail) => {
    replaceTool(rawTool(kind));
    const first = render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    const detail = () => kind === "exec" ? first.container.querySelector<HTMLButtonElement>(".exec-cell-header-button")!
      : first.container.querySelector<HTMLButtonElement>(".activity-cell-main-button")!;
    fireEvent.click(detail());
    if (closeDetail) fireEvent.click(detail());
    complete();first.rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name:"收起处理步骤" })).toBeTruthy();
    expect(detail().getAttribute("aria-expanded")).toBe(String(!closeDetail));
    done();first.rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    first.unmount();
    render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name:"收起处理步骤" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name:"收起处理步骤" }));
    expect(screen.getByRole("button", { name:"展开处理步骤" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name:"展开处理步骤" }));
    const restored = document.querySelector(kind === "exec" ? ".exec-cell-header-button" : ".activity-cell-main-button");
    expect(restored?.getAttribute("aria-expanded")).toBe(String(!closeDetail));
  });

  it.each([false,true])("does not infer protected MCP reading from a tool name, transcript=%s", (child) => {
    replaceTool({ id:"read", name:"mcp__pretend__lookup", activityKind:"genericTool", resultKind:"generic", args:{},
      status:"success", startedAt:3, finishedAt:4, outputPreview:"Ordinary generic evidence" });
    const {rerender}=render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    fireEvent.click(screen.getByRole("button", { name:"展开活动详情" }));
    complete();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name:"展开处理步骤" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name:"展开处理步骤" }));
    expect(screen.getByText("Ordinary generic evidence")).toBeTruthy();
  });

  it("protects only the last raw-read turn in its conversation and gives the parent toggle priority", () => {
    replaceTool(rawTool("exec"));
    const runningFirst = turn();
    const view = render(<ChatTurn turn={runningFirst} conversationId="lifecycle" />);
    fireEvent.click(view.container.querySelector(".exec-cell-header-button")!);
    complete();
    const settledFirst = turn();
    const runningSecond = { ...runningFirst, id:"assistant-next", committedCells:runningFirst.committedCells.map(cell => cell.kind === "exec"
      ? { ...cell, id:"second-exec", command:"printf 'second'" } : { ...cell, id:`second-${cell.id}` }) };
    const pair = (second = runningSecond) => <><ChatTurn key="first" turn={settledFirst} conversationId="lifecycle" />
      <ChatTurn key="second" turn={second} conversationId="lifecycle" /></>;
    view.rerender(pair());
    const first = () => view.container.querySelector<HTMLElement>('[data-message-id="assistant"]')!;
    const second = () => view.container.querySelector<HTMLElement>('[data-message-id="assistant-next"]')!;
    expect(within(first()).getByLabelText("Agent 处理进度").getAttribute("data-collapsed")).toBe("false");
    fireEvent.click(second().querySelector(".exec-cell-header-button")!);
    expect(within(first()).getByLabelText("Agent 处理进度").getAttribute("data-collapsed")).toBe("true");
    const settledSecond = { ...runningSecond, status:"completed" as const, finalAnswerCell:settledFirst.finalAnswerCell };
    view.rerender(pair(settledSecond));
    expect(within(second()).getByRole("button", { name:"收起处理步骤" })).toBeTruthy();
    fireEvent.click(within(second()).getByRole("button", { name:"收起处理步骤" }));
    view.rerender(pair(settledSecond));
    expect(within(second()).getByLabelText("Agent 处理进度").getAttribute("data-collapsed")).toBe("true");
  });

  it.each([false,true])("keeps the parent's explicit live toggle across completion, transcript=%s", (child) => {
    const {rerender}=render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    fireEvent.click(screen.getByRole("button", { name:"收起处理步骤" }));
    fireEvent.click(screen.getByRole("button", { name:"展开处理步骤" }));
    complete();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name:"收起处理步骤" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name:"收起处理步骤" }));
    done();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name:"展开处理步骤" })).toBeTruthy();
  });

  it("folds a child work record when its complete final item arrives", () => {
    const {rerender}=render(<ChatTurn turn={turn()} isTranscriptMode />);
    complete();rerender(<ChatTurn turn={turn()} isTranscriptMode />);
    expect(screen.queryByText("我会先查询来源并核对日期。")).toBeNull();
    expect(screen.getByText("完成验证。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button",{name:"展开处理步骤"}));
    done();rerender(<ChatTurn turn={turn()} isTranscriptMode />);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(screen.getByRole("button",{name:"收起处理步骤"})).toBeTruthy();
  });

  it.each([[false,false],[false,true],[true,false],[true,true]])("restores child reading after the parent folds across virtual remount, offscreen=%s transcript=%s", (offscreen, child) => {
    useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant" ? { ...message,
      blocks: message.blocks?.map((block) => block.type === "tool_call" ? { ...block, record: { ...block.record, outputPreview: "Remounted details" } } : block) } : message) }));
    const first = render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    fireEvent.click(screen.getByRole("button", { name:"展开活动详情" }));
    expect(screen.getByText("Remounted details")).toBeTruthy();
    if (offscreen) first.unmount();
    complete();
    if (!offscreen) {
      first.rerender(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
      expect(screen.getByRole("button", { name:"展开处理步骤" })).toBeTruthy();
      first.unmount();
    }
    const restored = render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name:"展开处理步骤" })).toBeTruthy();
    expect(screen.queryByText("Remounted details")).toBeNull();
    restored.unmount();
    done();
    render(<ChatTurn turn={turn()} conversationId="lifecycle" isTranscriptMode={child} />);
    expect(screen.getByRole("button", { name:"展开处理步骤" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name:"展开处理步骤" }));
    expect(screen.getByText("Remounted details")).toBeTruthy();
  });

  it("revokes final qualification when the same provider item is reclassified as commentary", () => {
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
    handle({type:"item.completed",conversation_id:"lifecycle",message_id:"assistant",turn_id:"actual-turn",
      item:{id:"model-final",type:"agent_message",text:"继续模型采样。",source:"commentary",status:"completed"}});
    rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(turn().finalAnswerCell).toBeNull();
    complete();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
  });

  it("does not render child dispatch as a human bubble or move assistant commentary into user role", () => {
    const {container}=render(<ChatTurn turn={turn()} isTranscriptMode />);
    expect(screen.queryByText("父任务指派：检查渲染")).toBeNull();
    expect(container.querySelector(".user-cell-wrap,.agent-loop-user-cell")).toBeNull();
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(useAppStore.getState().messages[0].role).toBe("user");
    expect(useAppStore.getState().messages[1].role).toBe("assistant");
  });

  it("restores completed child history collapsed while keeping its real dispatch in the transcript data", () => {
    const messages=hydrateMessages([
      {id:"dispatch",role:"user",content:"真实父派遣指令",timestamp:1},
      {id:"child",role:"assistant",turn_id:"child-turn",content:"真实子任务结果",terminal_status:"completed",timestamp:2,blocks:[
        {type:"text",item_id:"commentary",content:"子任务说明",source:"commentary",status:"completed",is_streaming:false},
        {type:"text",item_id:"result",content:"真实子任务结果",source:"model_final",status:"completed",is_streaming:false},
      ]},
    ]);
    const {container}=render(<ChatTurn turn={projectMessagesToTurns(messages,false)[0]} isTranscriptMode />);
    expect(container.querySelector(".user-cell-wrap")).toBeNull();
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
    expect(screen.getByText("真实子任务结果")).toBeTruthy();
    expect(screen.queryByText("子任务说明")).toBeNull();
    expect(messages[0].content).toBe("真实父派遣指令");
  });

  it.each(["normal","summary","verbose"] as const)("collapses the completed final item in %s mode before terminal delivery", (mode) => {
    useAppStore.setState({viewMode:mode});
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
    done();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:"展开处理步骤"})).toBeTruthy();
  });

  it("keeps the completed boundary and the later manual choice when mode/default props change", () => {
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    for (const mode of ["verbose", "summary", "normal"] as const) {
      useAppStore.setState({viewMode:mode});
      rerender(<ChatTurn turn={turn()} defaultProcessExpanded={false} />);
      rerender(<ChatTurn turn={turn()} defaultProcessExpanded />);
      expect(screen.getByRole("button", {name:"展开处理步骤"})).toBeTruthy();
    }
    fireEvent.click(screen.getByRole("button", {name:"展开处理步骤"}));
    done();
    for (const mode of ["summary", "verbose", "normal"] as const) {
      useAppStore.setState({viewMode:mode});
      rerender(<ChatTurn turn={turn()} defaultProcessExpanded={false} />);
      expect(screen.getByRole("button", {name:"收起处理步骤"})).toBeTruthy();
    }
  });

  it("temporarily exposes explicit search results without changing the completed disclosure choice", () => {
    useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant" ? { ...message,
      blocks: message.blocks?.map((block) => block.type === "tool_call" ? { ...block, record: { ...block.record, outputPreview: "Searched tool details" } } : block) } : message) }));
    const {rerender}=render(<TranscriptSearchContext.Provider value><ChatTurn turn={turn()} conversationId="lifecycle" /></TranscriptSearchContext.Provider>);
    fireEvent.click(screen.getByRole("button", { name:"收起活动详情" }));
    complete();rerender(<TranscriptSearchContext.Provider value><ChatTurn turn={turn()} conversationId="lifecycle" /></TranscriptSearchContext.Provider>);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    done();rerender(<TranscriptSearchContext.Provider value={false}><ChatTurn turn={turn()} conversationId="lifecycle" /></TranscriptSearchContext.Provider>);
    expect(screen.queryByText("我会先查询来源并核对日期。")).toBeNull();
    expect(screen.getByRole("button", {name:"展开处理步骤"})).toBeTruthy();
    fireEvent.click(screen.getByRole("button", {name:"展开处理步骤"}));
    expect(screen.getByText("Searched tool details")).toBeTruthy();
  });

  it.each(["failed","partial","cancelled"] as const)("reopens automatically folded evidence when terminal status becomes %s", (status) => {
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    expect(screen.queryByText("我会先查询来源并核对日期。")).toBeNull();
    handle({type:"done",conversation_id:"lifecycle",message_id:"assistant",turn_id:"actual-turn",status,reason:status,usage:{}});
    rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(screen.getByLabelText("Agent 处理进度").getAttribute("data-collapsed")).toBe("false");
  });

  it.each(["failed","partial","cancelled"] as const)("preserves an explicit collapse after %s and still lets the reader reopen", (status) => {
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    fireEvent.click(screen.getByRole("button",{name:"展开处理步骤"}));
    fireEvent.click(screen.getByRole("button",{name:"收起处理步骤"}));
    handle({type:"done",conversation_id:"lifecycle",message_id:"assistant",turn_id:"actual-turn",status,reason:status,usage:{}});
    rerender(<ChatTurn turn={turn()} />);
    expect(screen.queryByText("我会先查询来源并核对日期。")).toBeNull();
    fireEvent.click(screen.getByRole("button",{name:"展开处理步骤"}));
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("保留的草稿");
  });

  it.each(["failed","partial","cancelled"] as const)("preserves an explicit expansion after %s", (status) => {
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    fireEvent.click(screen.getByRole("button",{name:"展开处理步骤"}));
    handle({type:"done",conversation_id:"lifecycle",message_id:"assistant",turn_id:"actual-turn",status,reason:status,usage:{}});
    rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("保留的草稿");
  });

  it("reopens automatic disclosure when another live model item revokes the completed final qualification", () => {
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    handle({type:"item.started",conversation_id:"lifecycle",message_id:"assistant",turn_id:"actual-turn",item:{id:"continued",type:"agent_message",text:"",status:"in_progress"}});
    useAppStore.getState().appendAgentMessageDelta("continued","继续核对","lifecycle","assistant");
    rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(screen.getByLabelText("Agent 处理进度").getAttribute("data-collapsed")).toBe("false");
  });
});
