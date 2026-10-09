/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatTurn } from "./ChatTurn";
import { handleChatStreamEvent } from "../chatStreamEvents";
import { projectMessagesToTurns } from "../chatSurfaceState";
import { hydrateMessages } from "../transcriptHydration";
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

beforeEach(() => useAppStore.setState({conversationId:"lifecycle",pendingConversationSwitchId:null,isStreaming:true,
  conversationStreaming:{lifecycle:true},conversationMessages:{},sideChats:{},draft:"保留的草稿",viewMode:"normal",messages:[
    {id:"user",role:"user",content:"父任务指派：检查渲染",timestamp:1},
    {id:"assistant",role:"assistant",content:"",timestamp:2,isStreaming:true,turnId:"actual-turn",blocks:[
      {type:"text",itemId:"commentary",content:"我会先查询来源并核对日期。",source:"commentary",status:"completed",isStreaming:false},
      {type:"tool_call",record:{id:"read",name:"read_file",args:{file_path:"README.md"},status:"success",startedAt:3,finishedAt:4}},
    ]},
  ]}));
afterEach(cleanup);

describe("real item/terminal disclosure and child roles", () => {
  it("settles automatic disclosure after the completed answer's first commit", () => {
    const committedDisclosure: string[] = [];
    const captureCommit = () => {
      if (screen.queryByText("完成验证。")) {
        committedDisclosure.push(screen.getByLabelText("Agent 处理进度").getAttribute("data-collapsed")!);
      }
    };
    const {rerender}=render(<Profiler id="actual-disclosure-commit" onRender={captureCommit}><ChatTurn turn={turn()} /></Profiler>);
    complete();
    rerender(<Profiler id="actual-disclosure-commit" onRender={captureCommit}><ChatTurn turn={turn()} /></Profiler>);
    expect(committedDisclosure[0]).toBe("false");
    expect(committedDisclosure.at(-1)).toBe("true");
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

  it("retains the work area when a reader explicitly discloses a tool's details", () => {
    useAppStore.setState((state) => ({ messages: state.messages.map((message) => message.role === "assistant" ? { ...message,
      blocks: message.blocks?.map((block) => block.type === "tool_call" ? { ...block, record: { ...block.record, outputPreview: "Actual file contents" } } : block) } : message) }));
    const {rerender}=render(<ChatTurn turn={turn()} />);
    fireEvent.click(screen.getByRole("button",{name:"展开活动详情"}));
    expect(screen.getByText("Actual file contents")).toBeTruthy();
    complete();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:"收起处理步骤"})).toBeTruthy();
    expect(screen.getByText("我会先查询来源并核对日期。")).toBeTruthy();
    expect(screen.getByText("Actual file contents")).toBeTruthy();
    done();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByText("Actual file contents")).toBeTruthy();
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

  it.each(["normal","summary","verbose"] as const)("uses the %s disclosure preference for final item before terminal delivery", (mode) => {
    useAppStore.setState({viewMode:mode});
    const {rerender}=render(<ChatTurn turn={turn()} />);
    complete();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:mode==='verbose'?"收起处理步骤":"展开处理步骤"})).toBeTruthy();
    done();rerender(<ChatTurn turn={turn()} />);
    expect(screen.getByRole("button",{name:mode==='verbose'?"收起处理步骤":"展开处理步骤"})).toBeTruthy();
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
