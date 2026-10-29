/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FooterRow } from "./FooterRow";
import { useAppStore } from "../stores";
// These cases exercise the supported literal-input mode. Rich editing has its own document/IME suite.
beforeEach(() => useAppStore.setState({ composerPlainTextMode: true, composerDraftsReady: true }));
vi.mock("./RichComposerEditor", async () => ({ RichComposerEditor: (await import("../../test/literal-composer-editor")).LiteralComposerEditor }));

import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { ComposerAddMenu } from "./ComposerAddMenu";
import { ComposerTextarea } from "./ComposerTextarea";
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
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, desktop: () => undefined, pickDirectory: vi.fn(), remoteDirectoryEntries: remote.list }));
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
  render(<ComposerAddMenu workspaceRoot="C:/menu-project" planMode={false} planAvailable showGoal goalAvailable onFiles={files} onGoal={vi.fn()} onPlan={vi.fn()} onPlugin={vi.fn()} onClose={close} />);
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
  render(<><ComposerTextarea value="Keep this draft" onChange={() => {}} onSubmit={() => {}} /><FooterRow sendState="idle" onSend={()=>{}} /></>);
  act(()=>window.dispatchEvent(new Event("composer:open-add-menu")));
  expect(screen.getByRole("menu",{name:"添加内容"})).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("textbox"));
  expect(screen.getByRole("menuitem",{name:"文件和文件夹"}).getAttribute("data-highlighted")).toBe("true");
});

it("keeps composer focus while suggestion arrows loop and Enter activates the highlighted action", async () => {
  const submit = vi.fn();
  const view = render(<><ComposerTextarea value="Keep this draft" onChange={() => {}} onSubmit={submit} /><FooterRow sendState="idle" onSend={submit} /></>);
  const composer = screen.getByRole("textbox");
  const trigger = screen.getByRole("button", { name: "添加文件等内容" });
  expect(fireEvent.mouseDown(trigger)).toBe(false);
  fireEvent.click(trigger);
  const first = screen.getByRole("menuitem", { name: "文件和文件夹" });
  const last = await screen.findByRole("menuitem", { name: /Notes/ });
  expect(document.activeElement).toBe(composer);
  expect(fireEvent.keyDown(composer, { key: "ArrowUp" })).toBe(false);
  expect(last.getAttribute("data-highlighted")).toBe("true");
  expect(fireEvent.keyDown(composer, { key: "ArrowDown" })).toBe(false);
  expect(first.getAttribute("data-highlighted")).toBe("true");
  for (const event of [{ key: "Home" }, { key: "End" }, { key: "Tab" }, { key: "Tab", shiftKey: true }]) {
    expect(fireEvent.keyDown(composer, event)).toBe(true);
    expect(document.activeElement).toBe(composer);
    expect(first.getAttribute("data-highlighted")).toBe("true");
    expect(screen.getByRole("menu", { name: "添加内容" })).toBeTruthy();
  }
  const filePicker = vi.fn();
  view.container.querySelector('input[type="file"]')!.addEventListener("click", filePicker);
  expect(fireEvent.keyDown(composer, { key: "Enter" })).toBe(false);
  expect(filePicker).toHaveBeenCalledOnce();
  expect(submit).not.toHaveBeenCalled();
  expect(screen.queryByRole("menu")).toBeNull();
  fireEvent.click(trigger);
  expect(fireEvent.keyDown(composer, { key: "Escape" })).toBe(false);
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(composer);
  expect(useAppStore.getState().draft).toBe("Keep this draft");
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
});

it("skips disabled actions and leaves typing and Space to the composer rather than menu typeahead", async () => {
  const plan = vi.fn(), close = vi.fn();
  render(<ComposerAddMenu workspaceRoot="C:/menu-project" planMode={false} planAvailable showGoal goalAvailable={false}
    onFiles={vi.fn()} onGoal={vi.fn()} onPlan={plan} onPlugin={vi.fn()} onClose={close} />);
  await screen.findByRole("menuitem", { name: /Notes/ });
  const first = screen.getByRole("menuitem", { name: "文件和文件夹" }), item = screen.getByRole("menuitem", { name: /^计划模式/ });
  for (const key of ["p", "l", "a", "n", " ", "m"]) expect(fireEvent.keyDown(window, { key })).toBe(true);
  expect(first.getAttribute("data-highlighted")).toBe("true");
  expect(plan).not.toHaveBeenCalled();
  expect(close).not.toHaveBeenCalled();
  fireEvent.keyDown(window, { key: "ArrowDown" });
  expect(item.getAttribute("data-highlighted")).toBe("true");
  fireEvent.keyDown(window, { key: "Enter" });
  expect(plan).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledWith(true);
});

it("preserves outside focus and closes stale add controls while switching conversation owners", () => {
  render(<><ComposerTextarea value="Keep this draft" onChange={() => {}} onSubmit={() => {}} /><FooterRow sendState="idle" onSend={() => {}} /><button>Outside target</button></>);
  const trigger = screen.getByRole("button", { name: "添加文件等内容" }), outside = screen.getByRole("button", { name: "Outside target" });
  fireEvent.click(trigger);
  act(() => outside.focus());
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(outside);
  fireEvent.click(trigger);
  act(() => useAppStore.setState({ pendingConversationSwitchId: "next-owner" }));
  expect(screen.queryByRole("menu")).toBeNull();
  expect((trigger as HTMLButtonElement).disabled).toBe(true);
  expect(useAppStore.getState().draft).toBe("Keep this draft");
});

it.each(["client", "canonical", "submitting"] as const)("disables plan/goal mouse and keyboard actions for the existing %s lock and enables them when it clears", async phase => {
  const context = { id: "mode-draft", workspaceRoot: "C:/menu-project", bindWorkspace: true, appMode: "cowork" as const,
    permissionMode: "confirm" as const, permissionEdited: false, modelSelection: { mode: "default" as const },
    selectionLoading: false, selectionError: null, materializing: phase !== "submitting",
    ...(phase === "canonical" ? { conversationId: "menu-owner", adopted: true } : {}) };
  useAppStore.setState({ conversationId: phase === "client" ? null : "menu-owner", activeConversationDraftId: phase === "client" ? context.id : null,
    conversationDrafts: phase === "submitting" ? {} : { [context.id]: context }, agentMode: "build", goalMode: true,
    runtimeCapabilities: { feature_flags: { goals: { enabled: true } } } });
  const view = render(<FooterRow submitting={phase === "submitting"} sendState="idle" onSend={() => {}} />);
  const clearGoal = screen.getByRole("button", { name: "清除目标" }) as HTMLButtonElement;
  expect(clearGoal.disabled).toBe(true); fireEvent.click(clearGoal); expect(useAppStore.getState().goalMode).toBe(true);
  act(() => useAppStore.setState({ goalMode: false, agentMode: "plan" }));
  expect((screen.getByRole("button", { name: "关闭计划模式" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
  const plan = screen.getByRole("menuitem", { name: /^计划模式/ }) as HTMLButtonElement;
  const goal = screen.getByRole("menuitem", { name: /^目标/ }) as HTMLButtonElement;
  expect(plan.disabled).toBe(true); expect(goal.disabled).toBe(true);
  const plugin = await screen.findByRole("menuitem", { name: /Notes/ });
  fireEvent.keyDown(window, { key: "ArrowDown" });
  expect(plugin.getAttribute("data-highlighted")).toBe("true");
  expect(plan.getAttribute("aria-current")).toBeNull(); expect(goal.getAttribute("aria-current")).toBeNull();
  fireEvent.click(plan); fireEvent.click(goal);
  expect(useAppStore.getState()).toMatchObject({ agentMode: "plan", goalMode: false, draft: "Keep this draft" });
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  act(() => useAppStore.setState({ conversationDrafts: phase === "submitting" ? {} : { [context.id]: { ...context, materializing: false } } }));
  view.rerender(<FooterRow submitting={false} sendState="idle" onSend={() => {}} />);
  expect(plan.disabled).toBe(false); expect(goal.disabled).toBe(false);
  expect((screen.getByRole("button", { name: "关闭计划模式" }) as HTMLButtonElement).disabled).toBe(false);
});
