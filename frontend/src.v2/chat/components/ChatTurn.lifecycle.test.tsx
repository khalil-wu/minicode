/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("keeps explicitly opened tool details visible when the final answer arrives", () => {
    useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant" ? { ...message,
      blocks: message.blocks?.map((block) => block.type === "tool_call" ? { ...block, record: { ...block.record, outputPreview: "Actual file contents" } } : block) } : message) }));
    const {rerender}=render(<ChatTurn turn={turn()} conversationId="lifecycle" />);
    fireEvent.click(screen.getByRole("button",{name:"展开活动详情"}));
    expect(screen.getByText("Actual file contents")).toBeTruthy();
    complete();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" />);
    expect(screen.getByRole("button",{name:"收起处理步骤"})).toBeTruthy();
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(screen.getByText("Actual file contents")).toBeTruthy();
    done();rerender(<ChatTurn turn={turn()} conversationId="lifecycle" />);
    expect(screen.getByText("Actual file contents")).toBeTruthy();
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

  it.each([false,true])("preserves pre-final reading across virtual remount with completion offscreen=%s", (offscreen) => {
    useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant" ? { ...message,
      blocks: message.blocks?.map((block) => block.type === "tool_call" ? { ...block, record: { ...block.record, outputPreview: "Remounted details" } } : block) } : message) }));
    const first = render(<ChatTurn turn={turn()} conversationId="lifecycle" />);
    fireEvent.click(screen.getByRole("button", { name:"展开活动详情" }));
    expect(screen.getByText("Remounted details")).toBeTruthy();
    if (offscreen) first.unmount();
    complete();
    if (!offscreen) {
      first.rerender(<ChatTurn turn={turn()} conversationId="lifecycle" />);
      expect(screen.getByRole("button", { name:"收起处理步骤" })).toBeTruthy();
      first.unmount();
    }
    const restored = render(<ChatTurn turn={turn()} conversationId="lifecycle" />);
    expect(screen.getByRole("button", { name:"收起处理步骤" })).toBeTruthy();
    expect(screen.getByText("Remounted details")).toBeTruthy();
    restored.unmount();
    done();
    render(<ChatTurn turn={turn()} conversationId="lifecycle" />);
    expect(screen.getByRole("button", { name:"收起处理步骤" })).toBeTruthy();
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
    const {rerender}=render(<TranscriptSearchContext.Provider value><ChatTurn turn={turn()} /></TranscriptSearchContext.Provider>);
    complete();rerender(<TranscriptSearchContext.Provider value><ChatTurn turn={turn()} /></TranscriptSearchContext.Provider>);
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    done();rerender(<TranscriptSearchContext.Provider value={false}><ChatTurn turn={turn()} /></TranscriptSearchContext.Provider>);
    expect(screen.queryByText("我会先查询来源并核对日期。")).toBeNull();
    expect(screen.getByRole("button", {name:"展开处理步骤"})).toBeTruthy();
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
