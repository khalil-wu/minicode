/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HeaderBar } from "./HeaderBar";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import { emptyComposerDraft, flushComposerDrafts } from "../stores/composer-drafts";
import { handleSessionEvent } from "../chat/sessionEvents";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { ServerEvent } from "../protocol/events";
import { ComposerTextarea } from "../composer/ComposerTextarea";
import { richComposerSchema as schema, richDocumentState } from "../composer/rich-composer-document";
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: null }) }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(), fetchDraftModelSelection: vi.fn(async () => ({
  current_model: "fixture", available_models: ["fixture"], available_permission_modes: ["confirm"], feature_flags: {}, composer_commands: [],
})) }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: vi.fn(() => true), sendClientCommandAwaitResult: vi.fn() }));
const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never;
beforeEach(async () => {
  await waitComposerDraftRecovery(); vi.clearAllMocks(); localStorage.clear();
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: "A", activeConversationDraftId: null, conversationDrafts: {},
    homeConversationDraftIds: {}, conversations: [{ id: "A", title: "A", workspaceRoot: "C:/project" }, { id: "B", title: "B", workspaceRoot: "C:/project" }],
    messages: [], conversationMessages: {}, conversationStreaming: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {},
    conversationAgentStates: {}, pendingConversationSwitchId: null, pendingConversationCreateId: null, workingDirectory: "C:/project",
    runtimeSession: null, editorTabs: [], activeTabPath: null, activeEditorPath: null, isConnected: true, composerDraftsReady: true });
});
afterEach(async () => { cleanup(); await flushComposerDrafts(); });

function DraftInput() {
  const draft = useAppStore(state => state.draft);
  const document = useAppStore(state => state.richDraft);
  const owner = useAppStore(state => state.activeConversationDraftId || state.conversationId || "");
  return <ComposerTextarea rich documentKey={owner} value={draft} richDocument={document}
    onChange={(value, _selection, rich) => useAppStore.getState().setDraft(value, rich)} onSubmit={() => {}} />;
}

it("returns through actual Back/Forward to an independent same-project draft with its original rich selection and file", async () => {
  const rect = new DOMRect(0, 0, 600, 80);
  Object.defineProperty(Range.prototype, "getClientRects", { configurable: true, value: () => [rect] });
  Object.defineProperty(Range.prototype, "getBoundingClientRect", { configurable: true, value: () => rect });
  vi.spyOn(window, "scrollBy").mockImplementation(() => {});
  render(<><HeaderBar leftPanelAvailable={false} leftPanelOpen={false} rightPanelAvailable={false} rightPanelOpen={false}
    onToggleLeftPanel={() => {}} onToggleRightPanel={() => {}} /><DraftInput /></>);
  let old = "", next = "";
  await act(async () => { old = useAppStore.getState().startConversationDraft({ workspaceRoot: "C:/project", bindWorkspace: true }); });
  const reference = { kind: "plugin" as const, name: "电脑", configName: "computer-use@runtime", path: "plugin://computer-use@runtime", pluginPath: "C:/plugins/computer-use", iconVariant: "composer" as const };
  const saved = { ...richDocumentState(schema.node("doc", null, schema.node("paragraph", null, [schema.text("prefix "), schema.nodes.context_mention.create({ reference }), schema.text(" tail")]))),
    selection: { start: 7, end: 7 } };
  const file = new File(["unsaved original bytes"], "original.txt", { type: "text/plain" });
  const attachment = { id: "original-file", name: file.name, type: file.type, size: file.size, localFile: file, status: "ready" as const };
  act(() => useAppStore.setState({ draft: saved.markdown, richDraft: saved, selectedMentions: [reference], attachments: [attachment] }));
  await act(async () => { next = useAppStore.getState().startConversationDraft({ workspaceRoot: "C:/project", bindWorkspace: true, forceNew: true }); });
  expect(next).not.toBe(old);
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: next, draft: "", selectedMentions: [], selectedSkills: [], attachments: [] });
  act(() => useAppStore.getState().setDraft("help me create a skill"));
  fireEvent.click(screen.getByRole("button", { name: /上一条访问的对话|previous/i }));
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: old, draft: saved.markdown, richDraft: saved, selectedMentions: [reference] });
  expect(useAppStore.getState().attachments[0].localFile).toBe(file);
  expect(window.getSelection()!.anchorOffset).toBe(7);
  fireEvent.click(screen.getByRole("button", { name: /下一条访问的对话|next/i }));
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: next, draft: "help me create a skill", attachments: [] });
  expect(sendClientCommand).not.toHaveBeenCalledWith(expect.objectContaining({ type: "user_message" }));
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
});

it("returns to an authored local draft and forwards to a real conversation through its causal switch, with zero create", async () => {
  render(<HeaderBar leftPanelAvailable={false} leftPanelOpen={false} rightPanelAvailable={false} rightPanelOpen={false}
    onToggleLeftPanel={() => {}} onToggleRightPanel={() => {}} />);
  let draft = "";
  await act(async () => { draft = useAppStore.getState().startConversationDraft({ workspaceRoot: "C:/project", bindWorkspace: true }); });
  act(() => useAppStore.getState().setDraft("keep this history draft"));
  act(() => useAppStore.getState().applyConversationSwitched({ conversationId: "B" }));
  fireEvent.click(screen.getByRole("button", { name: /上一条访问的对话|previous/i }));
  expect(useAppStore.getState()).toMatchObject({ activeConversationDraftId: draft, conversationId: null, draft: "keep this history draft" });
  fireEvent.click(screen.getByRole("button", { name: /下一条访问的对话|next/i }));
  const command = vi.mocked(sendClientCommand).mock.calls.find(([command]) => command.type === "conversation.switch")![0];
  expect(command).toMatchObject({ type: "conversation.switch", conversation_id: "B" });
  expect(useAppStore.getState().pendingConversationSwitchId).toBe("B");
  act(() => handleSessionEvent({ type: "conversation.switched", conversation_id: "B", client_command_id: command.client_command_id,
    client_command_type: "conversation.switch", conversation: { id: "B", title: "B", workspace_root: "C:/project", messages: [] } } as ServerEvent, buffers));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", activeConversationDraftId: null, pendingConversationSwitchId: null });
  expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
});
