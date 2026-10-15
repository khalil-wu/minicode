/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FooterRow } from "./FooterRow";
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { showPrompt } from "../overlays/DialogService";
import { ComposerAddMenu } from "./ComposerAddMenu";

vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => null }));
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: vi.fn(async (command) => ({
    type: "command.result", command: command.type, status: "ok", client_command_id: command.client_command_id,
    data: { conversation_id: command.conversation_id, agent_mode: command.agent_mode, revision: 1 },
  })),
  commandResultSucceeded: () => true,
}));
vi.mock("../overlays/DialogService", () => ({ showPrompt: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, pickDirectory: vi.fn() }));
vi.mock("./uploads", () => ({ uploadComposerFiles: vi.fn() }));

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({plugins:[
    {id:"notes@personal",name:"notes",displayName:"Notes",marketplace:"personal",enabled:true},
    {id:"disabled@personal",name:"disabled",marketplace:"personal",enabled:false},
  ]}),{status:200})));
  useAppStore.setState({ conversationId:"menu-owner", workingDirectory:"C:/menu-project", draft:"Keep this draft", selectedMentions:[],
    pendingPermissionModeChanges:{}, pendingAgentModeChanges:{}, latestAgentModeCommandIds:{}, conversationWorkbenchStates:{},
    pendingConversationSwitchId:null, permissionMode:"auto", agentMode:"build", runtimeCapabilities:null, availableModels:[], currentModel:"", contextUsage:null,
    pendingApproval:null, approvalQueue:[], activeGoal:null, prMonitor:null, prStatusIssue:null });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

it("adds the actual plugin registration to context without replacing a draft", async () => {
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button",{name:"添加文件等内容"}));
  const plugin=await screen.findByRole("menuitem",{name:"Notes"});
  expect(screen.queryByRole("menuitem",{name:"disabled"})).toBeNull();
  fireEvent.click(plugin);
  expect(useAppStore.getState().selectedMentions).toMatchObject([{kind:"plugin",configName:"notes@personal",path:"plugin://notes@personal"}]);
  expect(useAppStore.getState().draft).toBe("Keep this draft");
});

it("sends a goal to the task that opened the prompt after another task is selected", async () => {
  let resolve!: (text:string)=>void;
  vi.mocked(showPrompt).mockImplementation(()=>new Promise<string>(complete=>{resolve=complete;}));
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button",{name:"添加文件等内容"}));
  fireEvent.click(screen.getByRole("menuitem",{name:/^目标/}));
  await waitFor(()=>expect(showPrompt).toHaveBeenCalled());
  act(()=>useAppStore.setState({conversationId:"other-task",workingDirectory:"C:/other-project"}));
  await act(async()=>{resolve("Finish the alignment");});
  expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({type:"conversation.goal.set",conversation_id:"menu-owner",text:"Finish the alignment"}));
  expect(useAppStore.getState().draft).toBe("Keep this draft");
});

it("selects conversational plan mode independently of the permission profile", async () => {
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button",{name:"添加文件等内容"}));
  fireEvent.click(screen.getByRole("menuitem",{name:/^计划模式/}));
  expect(useAppStore.getState().agentMode).toBe("plan");
  expect(useAppStore.getState().permissionMode).toBe("auto");
  expect(screen.getByRole("button",{name:"关闭计划模式"})).toBeTruthy();
  expect(useAppStore.getState().draft).toBe("Keep this draft");
  expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
    type:"conversation.agent_mode.set", conversation_id:"menu-owner", agent_mode:"plan",
  }),"conversation.agent_mode.set");
  await waitFor(()=>expect(useAppStore.getState().pendingAgentModeChanges).toEqual({}));
});

it("opens the file-and-folder choices and invokes the selected real action",()=>{
  const files = vi.fn(), folder = vi.fn(), close = vi.fn();
  render(<ComposerAddMenu workspaceRoot="C:/menu-project" planMode={false} goalAvailable onFiles={files} onFolder={folder} onGoal={vi.fn()} onPlan={vi.fn()} onPlugin={vi.fn()} onClose={close} />);
  fireEvent.click(screen.getByRole("menuitem",{name:"文件和文件夹"}));
  expect(files).not.toHaveBeenCalled();
  expect(folder).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("menuitem",{name:"引用文件夹"}));
  expect(folder).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});

it("can leave conversational plan mode while retaining full access",async()=>{
  useAppStore.setState({permissionMode:"bypass",agentMode:"plan"});
  render(<FooterRow sendState="idle" onSend={()=>{}} />);
  fireEvent.click(screen.getByRole("button",{name:"添加文件等内容"}));
  const planMenuItem = screen.getByRole("menuitem",{name:/^计划模式/});
  expect(planMenuItem.textContent).toContain("关闭计划模式");
  fireEvent.click(planMenuItem);
  expect(useAppStore.getState().agentMode).toBe("build");
  expect(useAppStore.getState().permissionMode).toBe("bypass");
  await waitFor(()=>expect(useAppStore.getState().pendingAgentModeChanges).toEqual({}));
});

it("opens the real add menu from a source-card request",()=>{
  render(<FooterRow sendState="idle" onSend={()=>{}} />);
  act(()=>window.dispatchEvent(new Event("composer:open-add-menu")));
  expect(screen.getByRole("menu",{name:"添加内容"})).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("menuitem",{name:"文件"}));
});
