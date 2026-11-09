/* @vitest-environment jsdom */
import type { CSSProperties } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { SideChatPanel } from "../panels/SideChatPanel";
import { useAppStore } from "../stores";
import { handleControlEvent } from "./controlEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";

const mocks = vi.hoisted(() => ({ response: vi.fn(), command: vi.fn(() => true) }));
vi.mock("../protocol/ws-outbox", async original => ({ ...await original<Record<string, unknown>>(),
  sendPromptResponseCommand: mocks.response, sendClientCommand: mocks.command }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../composer/RichComposerEditor", async () => ({
  RichComposerEditor: (await import("../../test/literal-composer-editor")).LiteralComposerEditor,
}));

const receipt = (level = "success") => ({ type: "command.result", command: "control_response", level,
  message: level === "error" ? "Original approval was refused" : "", data: {} });
const receive = (conversationId = "A") => {
  const normalized = normalizeInboundServerEvent({ type: "control_request", request_id: `approval-${conversationId}`,
    conversation_id: conversationId, turn_id: `turn-${conversationId}`, message_id: `message-${conversationId}`,
    task_id: `task-${conversationId}`, replayed: true,
    request: { subtype: "can_use_tool", tool_name: "run_command", tool_use_id: `approval-${conversationId}`,
      input: { command: `echo ${conversationId}` }, command: ["echo", conversationId], display_command: `echo ${conversationId}`,
      cwd: "C:/approval-shortcuts", request_digest: "a".repeat(64), available_decisions: ["approved", "abort"] },
  });
  expect(normalized).not.toBeNull();
  handleControlEvent(normalized!);
};
const originalResponse = (conversationId: string, action = "approve") => ({
  type: "control_response", request_id: `approval-${conversationId}`, conversation_id: conversationId,
  turn_id: `turn-${conversationId}`, message_id: `message-${conversationId}`, task_id: `task-${conversationId}`,
  response: { subtype: "success", response: expect.objectContaining({ action, request_digest: "a".repeat(64),
    decision: action === "approve" ? "approved" : "abort" }) },
});

const receiveComputerApproval = (conversationId = "A") => {
  const normalized = normalizeInboundServerEvent({ type: "control_request", request_id: `app-${conversationId}`,
    conversation_id: conversationId, turn_id: `turn-${conversationId}`, message_id: `message-${conversationId}`,
    task_id: `task-${conversationId}`, replayed: true,
    request: { subtype: "elicitation", tool_use_id: `app-${conversationId}`, mode: "form", mcp_server_name: "cua_repl",
      schema: {}, question: "Allow the local fixture app?", prompt: "Allow the local fixture app?", _meta: {
        codex_approval_kind: "mcp_tool_call", connector_id: "computer-use", connector_name: "Computer Use",
        persist: ["session", "always"], riskLevel: "low", tool_params: { app: "fixture-app" },
        tool_params_display: [{ name: "app", display_name: "App", value: "Fixture App" }],
      } },
  });
  expect(normalized).not.toBeNull();
  handleControlEvent(normalized!);
};
const computerResponse = (conversationId: string, action = "accept") => ({
  type: "control_response", request_id: `app-${conversationId}`, conversation_id: conversationId,
  turn_id: `turn-${conversationId}`, message_id: `message-${conversationId}`, task_id: `task-${conversationId}`,
  response: { subtype: "success", response: action === "accept" ? { action, content: { persist: "session" } } : { action } },
});
const KeyboardHarness = () => { useKeyboardShortcuts(); return null; };

beforeEach(() => {
  localStorage.removeItem("minicode:has-approved-computer-use-app");
  mocks.response.mockReset().mockResolvedValue(receipt()); mocks.command.mockClear();
  useAppStore.setState({ conversationId: "A", workingDirectory: "C:/approval-shortcuts", pendingConversationSwitchId: null,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null,
    diffReviewQueue: [], diffReview: null, promptDrafts: {}, subagents: [], runtimeSession: null, runtimeCapabilities: null,
    messages: [], conversationMessages: {}, conversationAgentStates: {}, isStreaming: false, isConnected: false,
    sideChats: {}, activeSideChatId: null, composerDraftsReady: true, composerPlainTextMode: true,
    sideChatPendingContext: null, turnDiffs: {}, currentModel: "fixture", availableModels: ["fixture"],
    slashCommands: [], availableSkills: [], draft: "A unsent draft", attachments: [],
    settingsOpen: false, commandPaletteOpen: false, shortcutsHelpOpen: false, quickOpenVisible: false,
    skillsMarketplaceOpen: false, liveArtifactsOpen: false, agentEditorOpen: false });
});
afterEach(cleanup);

it.each([
  { name: "settings/projects/marketplace/automations hidden wrapper", props: { hidden: true } },
  { name: "inactive editor tab", props: { style: { display: "none" } as CSSProperties } },
  { name: "switch loading", props: { style: { visibility: "hidden" } as CSSProperties } },
  { name: "inactive side panel", props: { "aria-hidden": true } },
  { name: "inert surface", props: { inert: "" } },
])("does not submit a mounted approval from a $name and restores it when shown", async ({ props }) => {
  receive();
  const view = render(<><div {...props}><InlineAgentPrompt /></div><main data-testid="other">Other page</main></>);
  fireEvent.keyDown(screen.getByTestId("other"), { key: "Enter" });
  fireEvent.keyDown(screen.getByTestId("other"), { key: "Escape" });
  expect(mocks.response).not.toHaveBeenCalled();
  expect(useAppStore.getState().pendingApproval).toMatchObject({ requestId: "approval-A", conversationId: "A" });
  view.rerender(<><div><InlineAgentPrompt /></div><main data-testid="other">Other page</main></>);
  await act(async () => { fireEvent.keyDown(document.body, { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("A"));
});

it("preserves dialog/menu/editable and native Enter handling instead of approving the background", async () => {
  receive();
  const click = vi.fn();
  render(<><InlineAgentPrompt /><input data-testid="input" /><textarea data-testid="textarea" />
    <select data-testid="select"><option>Value</option></select><div contentEditable data-testid="editable" />
    <div role="dialog" data-testid="dialog" /><div role="menu" data-testid="menu" />
    <div role="listbox" data-testid="listbox" /><button onClick={click}>Native action</button><a href="#local">Local link</a></>);
  for (const kind of ["input", "textarea", "select", "editable", "dialog", "menu", "listbox"]) {
    for (const key of ["Enter", "Escape"]) fireEvent.keyDown(screen.getByTestId(kind), { key });
  }
  expect(fireEvent.keyDown(screen.getByRole("button", { name: "Native action" }), { key: "Enter" })).toBe(true);
  expect(fireEvent.keyDown(screen.getByRole("link", { name: "Local link" }), { key: "Enter" })).toBe(true);
  expect(mocks.response).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Native action" })); expect(click).toHaveBeenCalledOnce();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "允许使用工具" })); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("A"));
});

it("does not use a global body shortcut behind an active modal, and ignores a closed modal", async () => {
  receive();
  const view = render(<><InlineAgentPrompt /><div role="dialog" aria-modal="true">Unrelated modal</div></>);
  fireEvent.keyDown(document.body, { key: "Enter" }); fireEvent.keyDown(document.body, { key: "Escape" });
  expect(mocks.response).not.toHaveBeenCalled();
  view.rerender(<><InlineAgentPrompt /><div role="dialog" aria-modal="true" hidden>Closed modal</div></>);
  await act(async () => { fireEvent.keyDown(document.body, { key: "Escape" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("A", "reject"));
});

it("skips a hidden first owner and submits only the visible owner's original identity", async () => {
  receive(); receive("B");
  render(<><div style={{ display: "none" }}><InlineAgentPrompt conversationId="A" /></div><InlineAgentPrompt conversationId="B" /></>);
  await act(async () => { fireEvent.keyDown(document.body, { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("B"));
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "A", requestId: "approval-A" });
});

it("routes keyboard input to its own visible pane and never borrows another pane's approval", async () => {
  receive(); receive("B");
  render(<><section className="chat-pane" data-testid="main-pane"><InlineAgentPrompt conversationId="A" /></section>
    <section className="chat-pane" data-testid="side-pane"><InlineAgentPrompt conversationId="B" /></section>
    <section className="chat-pane" data-testid="no-approval">Other conversation</section></>);
  fireEvent.keyDown(screen.getByTestId("no-approval"), { key: "Enter" });
  expect(mocks.response).not.toHaveBeenCalled();
  await act(async () => { fireEvent.keyDown(screen.getByTestId("side-pane"), { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("B"));
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "A" });
});

it("uses the focused approval card when the same pane also contains a different pending review", async () => {
  receive();
  useAppStore.getState().setDiffReview({ requestId: "diff-A", conversationId: "A", diff: "+proposed change" });
  const view = render(<section className="chat-pane"><InlineAgentPrompt /></section>);
  const commandCard = view.container.querySelector(".inline-native-command-approval .inline-approval-main")!;
  await act(async () => { fireEvent.keyDown(commandCard, { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("A"));
  expect(useAppStore.getState().pendingDiffReview).toMatchObject({ requestId: "diff-A", conversationId: "A" });
});

it("does not transfer repeated global keys past a submitting first approval and permits an explicit retry", async () => {
  let finish!: (value: ReturnType<typeof receipt>) => void;
  mocks.response.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  receive(); receive("B");
  render(<><InlineAgentPrompt conversationId="A" /><InlineAgentPrompt conversationId="B" /></>);
  fireEvent.keyDown(document.body, { key: "Enter" });
  fireEvent.keyDown(document.body, { key: "Enter" }); fireEvent.keyDown(document.body, { key: "Escape" });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("A"));
  expect(useAppStore.getState().approvalQueue).toContainEqual(expect.objectContaining({ conversationId: "B" }));
  await act(async () => finish(receipt("error")));
  expect(screen.getByText("Original approval was refused").closest('[role="alert"]')).not.toBeNull();
  await act(async () => { fireEvent.keyDown(document.body, { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledTimes(2);
  expect(mocks.response.mock.calls[1][0]).toMatchObject(originalResponse("A"));
});

it("removes stale listeners on an owner switch and unmount, and respects repeat/composition/modifiers", async () => {
  receive(); receive("B");
  const view = render(<InlineAgentPrompt />);
  for (const extra of [{ repeat: true }, { isComposing: true }, { altKey: true }, { ctrlKey: true }, { metaKey: true }]) {
    fireEvent.keyDown(document.body, { key: "Enter", ...extra });
  }
  expect(mocks.response).not.toHaveBeenCalled();
  act(() => useAppStore.setState({ conversationId: "B" }));
  await act(async () => { fireEvent.keyDown(document.body, { key: "Escape" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("B", "reject"));
  view.unmount(); fireEvent.keyDown(document.body, { key: "Enter" });
  expect(mocks.response).toHaveBeenCalledOnce();
});

const streamingSideChat = () => useAppStore.setState({ activeSideChatId: "side", sideChats: {
  side: { id: "side", workspaceRoot: "C:/approval-shortcuts", parentConversationId: "A", messages: [],
    isStreaming: true, draft: "side unsent draft", attachments: [], contextRefs: [] },
} });

it("lets the real streaming side pane reject its own approval instead of stopping the conversation", async () => {
  streamingSideChat(); receive("side"); render(<SideChatPanel />);
  await act(async () => { fireEvent.keyDown(screen.getByRole("log", { name: "侧边对话历史" }), { key: "Escape" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("side", "reject"));
  expect(mocks.command).not.toHaveBeenCalled();
  expect(useAppStore.getState().sideChats.side.draft).toBe("side unsent draft");
});

it("keeps ordinary side-pane stop behavior when only the main conversation has an approval", () => {
  streamingSideChat(); receive(); render(<><InlineAgentPrompt /><SideChatPanel /></>);
  fireEvent.keyDown(screen.getByRole("log", { name: "侧边对话历史" }), { key: "Escape" });
  expect(mocks.response).not.toHaveBeenCalled();
  expect(mocks.command).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ type: "interrupt", conversation_id: "side" }));
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "A" });
});

it.each([
  { name: "settings hidden", props: { hidden: true } },
  { name: "inactive tab", props: { style: { display: "none" } as CSSProperties } },
  { name: "switch loading", props: { style: { visibility: "hidden" } as CSSProperties } },
  { name: "inactive panel", props: { "aria-hidden": true } },
  { name: "inert chat", props: { inert: "" } },
])("keeps native app access pending under $name and restores the original owner", async ({ props }) => {
  receiveComputerApproval();
  const view = render(<><div {...props}><InlineAgentPrompt /></div><main data-testid="other-page">Other page</main></>);
  for (const key of ["Enter", "Escape"]) fireEvent.keyDown(screen.getByTestId("other-page"), { key });
  expect(mocks.response).not.toHaveBeenCalled();
  expect(useAppStore.getState().pendingAskUser).toMatchObject({ requestId: "app-A", conversationId: "A" });
  expect(localStorage.getItem("minicode:has-approved-computer-use-app")).toBeNull();
  view.rerender(<><div><InlineAgentPrompt /></div><main data-testid="other-page">Other page</main></>);
  await act(async () => { fireEvent.keyDown(document.body, { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(computerResponse("A"));
});

it("keeps native app access out of unrelated modal, menu, editable and native Enter contexts", async () => {
  receiveComputerApproval();
  const view = render(<><InlineAgentPrompt /><div role="dialog" data-testid="app-dialog" />
    <div role="menu" data-testid="app-menu" /><div role="listbox" data-testid="app-list" />
    <input data-testid="app-input" /><div contentEditable data-testid="app-editable" />
    <button>Unrelated app action</button><a href="#local">App link</a></>);
  for (const target of ["app-dialog", "app-menu", "app-list", "app-input", "app-editable"]) {
    for (const key of ["Enter", "Escape"]) fireEvent.keyDown(screen.getByTestId(target), { key });
  }
  fireEvent.keyDown(screen.getByRole("button", { name: "Unrelated app action" }), { key: "Enter" });
  fireEvent.keyDown(screen.getByRole("link", { name: "App link" }), { key: "Enter" });
  expect(mocks.response).not.toHaveBeenCalled();
  view.rerender(<><InlineAgentPrompt /><div role="dialog" aria-modal="true">Visible app modal</div></>);
  fireEvent.keyDown(document.body, { key: "Enter" }); fireEvent.keyDown(document.body, { key: "Escape" });
  expect(mocks.response).not.toHaveBeenCalled();
  view.rerender(<><InlineAgentPrompt /><div role="dialog" aria-modal="true" hidden>Closed app modal</div></>);
  await act(async () => { fireEvent.keyDown(document.body, { key: "Escape" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(computerResponse("A", "decline"));
});

it("arbitrates generic and native app approvals through the same visible pane ownership", async () => {
  receive(); receiveComputerApproval("B");
  render(<><section className="chat-pane" data-testid="tool-pane"><InlineAgentPrompt conversationId="A" /></section>
    <section className="chat-pane" data-testid="app-pane"><InlineAgentPrompt conversationId="B" /></section></>);
  await act(async () => { fireEvent.keyDown(screen.getByTestId("app-pane"), { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(computerResponse("B"));
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "A", requestId: "approval-A" });
});

it("does not pass keys through a submitting native app request and keeps a refused request retryable", async () => {
  let finish!: (value: ReturnType<typeof receipt>) => void;
  mocks.response.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  receiveComputerApproval(); receive("B");
  const view = render(<><InlineAgentPrompt conversationId="A" /><InlineAgentPrompt conversationId="B" /></>);
  for (const extra of [{ repeat: true }, { isComposing: true }, { altKey: true }, { ctrlKey: true }, { metaKey: true }]) {
    fireEvent.keyDown(document.body, { key: "Enter", ...extra });
  }
  expect(mocks.response).not.toHaveBeenCalled();
  fireEvent.keyDown(document.body, { key: "Enter" });
  fireEvent.keyDown(document.body, { key: "Enter" }); fireEvent.keyDown(document.body, { key: "Escape" });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(computerResponse("A"));
  await act(async () => finish(receipt("error")));
  expect(screen.getByText("Original approval was refused").closest('[role="alert"]')).not.toBeNull();
  expect(localStorage.getItem("minicode:has-approved-computer-use-app")).toBeNull();
  await act(async () => { fireEvent.keyDown(document.body, { key: "Enter" }); });
  expect(mocks.response).toHaveBeenCalledTimes(2);
  expect(mocks.response.mock.calls[1][0]).toMatchObject(computerResponse("A"));
  expect(useAppStore.getState().pendingApproval).toMatchObject({ conversationId: "B" });
  view.unmount(); fireEvent.keyDown(document.body, { key: "Enter" });
  expect(mocks.response).toHaveBeenCalledTimes(2);
});

it("lets a native side app approval consume Escape before side stop and global main stop", async () => {
  streamingSideChat(); useAppStore.setState({ isStreaming: true }); receiveComputerApproval("side");
  render(<><KeyboardHarness /><SideChatPanel /></>);
  await act(async () => { fireEvent.keyDown(screen.getByRole("log", { name: "侧边对话历史" }), { key: "Escape" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(computerResponse("side", "decline"));
  expect(mocks.command).not.toHaveBeenCalled();
  expect(useAppStore.getState().sideChats.side.draft).toBe("side unsent draft");
  expect(useAppStore.getState().isStreaming).toBe(true);
});

it("lets a generic document approval consume Escape before the global main interrupt listener", async () => {
  receive(); useAppStore.setState({ isStreaming: true }); render(<><KeyboardHarness /><InlineAgentPrompt /></>);
  await act(async () => { fireEvent.keyDown(document.body, { key: "Escape" }); });
  expect(mocks.response).toHaveBeenCalledExactlyOnceWith(originalResponse("A", "reject"));
  expect(mocks.command).not.toHaveBeenCalled();
  expect(useAppStore.getState().isStreaming).toBe(true);
});
