/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FooterRow } from "./FooterRow";
import { useAppStore } from "../stores";
// These cases exercise the supported literal-input mode. Rich editing has its own document/IME suite.
beforeEach(() => useAppStore.setState({ composerPlainTextMode: true }));
vi.mock("./RichComposerEditor", async () => ({ RichComposerEditor: (await import("../../test/literal-composer-editor")).LiteralComposerEditor }));

import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { ComposerAddMenu } from "./ComposerAddMenu";
const remote = vi.hoisted(() => ({ pick: vi.fn(), upload: vi.fn(), list: vi.fn() }));

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
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, pickDirectory: vi.fn(), remoteDirectoryEntries: remote.list }));
vi.mock("../overlays/RemoteFilePicker", () => ({ showRemoteFilePicker: remote.pick }));
vi.mock("./uploads", () => ({ uploadComposerFiles: vi.fn(), uploadComposerRemoteFiles: remote.upload }));

beforeEach(() => {
  remote.pick.mockReset(); remote.upload.mockReset(); remote.list.mockReset();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({plugins:[
    {id:"notes@personal",name:"notes",displayName:"Notes",marketplace:"personal",enabled:true},
    {id:"disabled@personal",name:"disabled",marketplace:"personal",enabled:false},
  ]}),{status:200})));
  useAppStore.setState({ conversationId:"menu-owner", workingDirectory:"C:/menu-project", draft:"Keep this draft", selectedMentions:[],
    pendingPermissionModeChanges:{}, pendingAgentModeChanges:{}, latestAgentModeCommandIds:{}, conversationWorkbenchStates:{},
    pendingConversationSwitchId:null, permissionMode:"auto", agentMode:"build", runtimeCapabilities:null, availableModels:[], currentModel:"", contextUsage:null,
    pendingApproval:null, approvalQueue:[], activeGoal:null, prMonitor:null, prStatusIssue:null });
  useAppStore.setState({ goalMode: false });
});
afterEach(() => { cleanup(); delete window.__MINICODE_RUNTIME__; vi.clearAllMocks(); vi.unstubAllGlobals(); });

const bindRemote = () => {
  window.__MINICODE_RUNTIME__ = { remoteHost: { id: "ssh-owner", displayName: "Remote", kind: "ssh", platform: "linux", home: "/home/dev", generation: "generation-1" } };
  useAppStore.setState({ conversationId: "remote-chat", workingDirectory: "/srv/project", draft: "Keep remote draft" });
};

it("exposes the source remote action only in a remote window and lists files on its original host", async () => {
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  expect(screen.queryByRole("menuitem", { name: "添加远程文件" })).toBeNull();
  cleanup(); bindRemote();
  remote.pick.mockResolvedValue(["/outside/报告.txt", "/srv/image.png"]);
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "添加远程文件" }));
  await waitFor(() => expect(remote.upload).toHaveBeenCalledWith(["/outside/报告.txt", "/srv/image.png"]));
  expect(remote.pick).toHaveBeenCalledWith(expect.objectContaining({ initialDirectoryPath: "/srv/project", platform: "linux", signal: expect.any(AbortSignal) }));
  await remote.pick.mock.calls[0][0].listDirectory("/outside");
  expect(remote.list).toHaveBeenCalledWith("/outside", false);
  expect(useAppStore.getState().draft).toBe("Keep remote draft");
});

it("retires the real remote picker across an A-to-B-to-A change", async () => {
  let finish!: (paths: string[] | null) => void;
  bindRemote();
  remote.pick.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "添加远程文件" }));
  await waitFor(() => expect(remote.pick).toHaveBeenCalledOnce());
  act(() => { useAppStore.setState({ conversationId: "other" }); useAppStore.setState({ conversationId: "remote-chat", draft: "newer draft" }); });
  expect(remote.pick.mock.calls[0][0].signal.aborted).toBe(true);
  await act(async () => { finish(["/outside/stale.txt"]); });
  expect(remote.upload).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("newer draft");
});

it("keeps a newer local picker active after an aborted remote chooser settles", async () => {
  let finish!: (paths: string[] | null) => void;
  bindRemote();
  remote.pick.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "添加远程文件" }));
  await waitFor(() => expect(remote.pick).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "文件和文件夹" }));
  await act(async () => { finish(null); });
  const file = new File(["newer local file"], "local.txt", { type: "text/plain" });
  fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [file] } });
  expect((await import("./uploads")).uploadComposerFiles).toHaveBeenCalledWith([file]);
  expect(remote.upload).not.toHaveBeenCalled();
});

it("adds the actual plugin registration to context without replacing a draft", async () => {
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button",{name:"添加文件等内容"}));
  const plugin=await screen.findByRole("menuitem",{name:"Notes"});
  expect(screen.queryByRole("menuitem",{name:"disabled"})).toBeNull();
  fireEvent.click(plugin);
  expect(useAppStore.getState().selectedMentions).toMatchObject([{kind:"plugin",configName:"notes@personal",path:"plugin://notes@personal"}]);
  expect(useAppStore.getState().draft).toBe("Keep this draft");
});

it("enters the source goal composer mode without replacing the original draft or opening a prompt", async () => {
  render(<FooterRow sendState="idle" onSend={() => {}} />);
  fireEvent.click(screen.getByRole("button",{name:"添加文件等内容"}));
  fireEvent.click(screen.getByRole("menuitem",{name:/^目标/}));
  expect(useAppStore.getState().goalMode).toBe(true);
  expect(sendClientCommand).not.toHaveBeenCalledWith(expect.objectContaining({type:"conversation.goal.set"}));
  expect(screen.getByRole("button",{name:"清除目标"})).toBeTruthy();
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

it("opens the real file picker directly without adding another file-and-folder submenu",()=>{
  const files = vi.fn(), close = vi.fn();
  render(<ComposerAddMenu workspaceRoot="C:/menu-project" planMode={false} goalAvailable onFiles={files} onGoal={vi.fn()} onPlan={vi.fn()} onPlugin={vi.fn()} onClose={close} />);
  fireEvent.click(screen.getByRole("menuitem",{name:"文件和文件夹"}));
  expect(files).toHaveBeenCalledOnce();
  expect(screen.queryByRole("menuitem", { name: "引用文件夹" })).toBeNull();
  expect(screen.queryByRole("button", { name: "管理插件" })).toBeNull();
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
  expect(document.activeElement).toBe(screen.getByRole("menuitem",{name:"文件和文件夹"}));
});
