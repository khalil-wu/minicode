/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HeaderBar } from "./HeaderBar";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import { emptyComposerDraft, flushComposerDrafts } from "../stores/composer-drafts";
import { handleSessionEvent } from "../chat/sessionEvents";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { ServerEvent } from "../protocol/events";
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
