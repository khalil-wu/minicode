/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
// These cases exercise the supported literal-input mode. Rich editing has its own document/IME suite.
beforeEach(() => useAppStore.setState({ composerPlainTextMode: true }));
vi.mock("../composer/RichComposerEditor", async () => ({ RichComposerEditor: (await import("../../test/literal-composer-editor")).LiteralComposerEditor }));

import { promptDraftKey } from "../stores/prompt-drafts";
import { resetPendingUserMessagesForTests } from "./sendChatMessage";
import { handleCommandResultEvent } from "./commandResultEvents";
import { notifyClientCommandReceived, registerWebSocketSender, resetPendingCommandResultsForTests } from "../protocol/ws-outbox";
import { PlanImplementationPrompt, pendingPlanImplementation } from "./PlanImplementationPrompt";
import { Composer } from "../composer/Composer";
import type { ChatMessage, ComposerAttachment } from "../stores/types";
import type { ClientCommand, CommandResultEvent } from "../protocol/events";

const wire = vi.hoisted(() => ({ commands: [] as ClientCommand[], toast: vi.fn() }));
vi.mock("../hooks/useWebSocket", async load => ({ ...await load<typeof import("../hooks/useWebSocket")>(),
  getWebSocket: () => ({ sessionId: "plan-prompt-session", send: (command: ClientCommand) => { wire.commands.push(command); return true; } }) }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: wire.toast }));
vi.mock("../protocol/api", async load => ({ ...await load<typeof import("../protocol/api")>(),
  fetchWithTimeout: vi.fn(async () => new Response('{"plugins":[]}', { status: 200 })) }));

const rawPlan = "## 实施步骤\n\n1. 修改真实调用链。\n2. 统一验证。";
const completed: ChatMessage = { id: "plan-answer", role: "assistant", timestamp: 1, turnId: "plan-turn",
  terminalStatus: "completed", content: `先核对目标。\n<proposed_plan>\n${rawPlan}\n</proposed_plan>\n再执行。` };
const plan = { messageId: completed.id, turnId: completed.turnId, plan: rawPlan };
const requestKey = promptDraftKey({ conversationId: "plan-owner", requestId: "implement-plan:plan-turn" });
const attachment = (id: string): ComposerAttachment => ({ id, name: `${id}.txt`, type: "text/plain", size: 4,
  status: "ready", artifactId: `artifact-${id}`, conversationId: id === "saved-attachment" ? "plan-owner" : "other-owner",
  attachment: { id: `artifact-${id}`, artifact_id: `artifact-${id}`, kind: "document", media_type: "text/plain", file_name: `${id}.txt` } });
const userCommands = () => wire.commands.filter((command): command is Extract<ClientCommand, { type: "user_message" }> => command.type === "user_message");
const modeCommands = () => wire.commands.filter(command => command.type === "conversation.agent_mode.set");
async function reply(command: ClientCommand, accepted = true) {
  const result: CommandResultEvent = { type: "command.result", command: command.type,
    client_command_id: command.client_command_id, level: accepted ? "success" : "error", message: accepted ? "" : "真实准入拒绝",
    data: command.type === "user_message" ? { admitted: accepted, conversation_id: command.conversation_id,
      user_message_id: command.user_message_id, assistant_message_id: command.assistant_message_id }
      : { conversation_id: "plan-owner", ...(command.type === "conversation.agent_mode.set" ? { agent_mode: command.agent_mode } : {}), revision: 2 } };
  await act(async () => { handleCommandResultEvent(result); });
}
const renderPrompt = () => render(<PlanImplementationPrompt owner="plan-owner" plan={plan} />);
const chooseImplement = () => fireEvent.click(screen.getByRole("radio", { name: /是的，执行这个计划/ }));

beforeEach(async () => {
  await waitComposerDraftRecovery();
  resetPendingUserMessagesForTests(); wire.commands = []; wire.toast.mockClear();
  useAppStore.setState({ conversationId: "plan-owner", workingDirectory: "C:/plan-owner", isConnected: true, composerDraftsReady: true,
    appMode: "cowork", agentMode: "plan", permissionMode: "confirm", draft: "原主草稿", attachments: [attachment("saved-attachment")],
    quotedMessage: { id: "draft-quote", role: "assistant", content: "原未发送引用" },
    selectedMentions: [], selectedSkills: [], promptDrafts: {}, messages: [completed], conversationMessages: {},
    conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, pendingAgentModeChanges: {}, latestAgentModeCommandIds: {},
    pendingPermissionModeChanges: {}, pendingConversationSwitchId: null, pendingConversationCreateId: null,
    isStreaming: false, conversationStreaming: {}, runtimeSession: null, pendingApproval: null, approvalQueue: [],
    pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], availableSkills: [], slashCommands: [],
    currentProvider: "custom", currentModel: "fixture-plan", availableModels: ["fixture-plan"],
    slashPanelOpen: false, mentionPanelOpen: false, sendShortcut: "enter", activeGoal: null,
    conversations: [{ id: "plan-owner", title: "Plan", updatedAt: "2026-10-13", agentMode: "plan", workspaceRoot: "C:/plan-owner", revision: 1 },
      { id: "other-owner", title: "Other", updatedAt: "2026-10-13", agentMode: "review", workspaceRoot: "C:/other-owner", revision: 1 }] });
  registerWebSocketSender(command => { wire.commands.push(command); return true; });
});
afterEach(async () => {
  cleanup();
  await act(async () => resetPendingCommandResultsForTests());
  resetPendingUserMessagesForTests(); registerWebSocketSender(null); vi.restoreAllMocks();
});

describe("Codex Wr completed-plan request", () => {
  it("extracts only a completed latest assistant plan in Plan mode", () => {
    expect(pendingPlanImplementation([completed], "plan", false)).toEqual(plan);
    expect(pendingPlanImplementation([completed], "build", false)).toBeNull();
    expect(pendingPlanImplementation([completed], "plan", true)).toBeNull();
    for (const changed of [{ terminalStatus: "failed" as const }, { terminalStatus: "interrupted" as const },
      { isStreaming: true }, { isThinkingStreaming: true }, { content: "<proposed_plan>unfinished" }]) {
      expect(pendingPlanImplementation([{ ...completed, ...changed }], "plan", false)).toBeNull();
    }
    expect(pendingPlanImplementation([completed, { id: "new-input", role: "user", content: "new", timestamp: 2 }], "plan", false)).toBeNull();
    expect(pendingPlanImplementation([{ ...completed, content: "```xml\n<proposed_plan>example</proposed_plan>\n```" }], "plan", false)).toBeNull();
  });

  it("sends the exact implementation prefix and plan once, without a quote, after real semantic admission", async () => {
    const saved = useAppStore.getState();
    renderPrompt(); chooseImplement(); chooseImplement();
    await waitFor(() => expect(userCommands()).toHaveLength(1));
    const command = userCommands()[0];
    expect(command).toMatchObject({ content: `PLEASE IMPLEMENT THIS PLAN:\n${rawPlan}`, display_content: `PLEASE IMPLEMENT THIS PLAN:\n${rawPlan}`,
      conversation_id: "plan-owner", agent_mode: "build", quoted_message: null, context_refs: [] });
    expect(command.attachments).toBeUndefined();
    act(() => notifyClientCommandReceived(command.client_command_id!));
    expect(modeCommands()).toHaveLength(0);
    expect(useAppStore.getState().promptDrafts[requestKey]?.planImplementationSettled).not.toBe(true);
    await reply(command);
    await waitFor(() => expect(modeCommands()).toHaveLength(1));
    await reply(modeCommands()[0]);
    await waitFor(() => expect(screen.queryByText("执行这个计划吗？")).toBeNull());
    const current = useAppStore.getState();
    expect(current.agentMode).toBe("build");
    expect(current.permissionMode).toBe("confirm");
    expect(current.draft).toBe(saved.draft);
    expect(current.attachments).toBe(saved.attachments);
    expect(current.quotedMessage).toBe(saved.quotedMessage);
    const local = current.messages.filter(message => message.role === "user");
    expect(local).toHaveLength(1);
    expect(local[0].content).toBe(`PLEASE IMPLEMENT THIS PLAN:\n${rawPlan}`);
    expect(local[0].quotedMessage).toBeFalsy();
  });

  it("sends feedback alone in Plan mode and preserves the main draft and attachments", async () => {
    const saved = useAppStore.getState(); renderPrompt();
    fireEvent.change(screen.getByRole("textbox", { name: "回答当前问题" }), { target: { value: "  先调整第二步。  " } });
    fireEvent.click(screen.getByRole("button", { name: "提交" }));
    await waitFor(() => expect(userCommands()).toHaveLength(1));
    expect(userCommands()[0]).toMatchObject({ content: "先调整第二步。", display_content: "先调整第二步。", agent_mode: "plan", quoted_message: null });
    expect(userCommands()[0].content).not.toContain(rawPlan);
    await reply(userCommands()[0]);
    await waitFor(() => expect(screen.queryByText("执行这个计划吗？")).toBeNull());
    expect(modeCommands()).toHaveLength(0);
    expect(useAppStore.getState()).toMatchObject({ agentMode: "plan", draft: saved.draft, attachments: saved.attachments, quotedMessage: saved.quotedMessage });
  });

  it("Skip sends no message and restores the actual composer with its draft, quote and attachment", async () => {
    const saved = useAppStore.getState(); render(<Composer />);
    expect(screen.queryByRole("textbox", { name: "消息输入" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "跳过" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "消息输入" })).toBeTruthy());
    expect(userCommands()).toEqual([]);
    expect(modeCommands()).toEqual([]);
    expect((screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement).value).toBe(saved.draft);
    expect(useAppStore.getState().attachments).toBe(saved.attachments);
    expect(useAppStore.getState().quotedMessage).toBe(saved.quotedMessage);
    expect(useAppStore.getState().agentMode).toBe("plan");
    expect(useAppStore.getState().promptDrafts[requestKey].planImplementationSettled).toBe(true);
  });

  it("Escape only changes mode and settles after its ACK", async () => {
    const saved = useAppStore.getState(); const view = renderPrompt();
    fireEvent.keyDown(view.container.querySelector("form")!, { key: "Escape" });
    expect(userCommands()).toEqual([]);
    expect(modeCommands()).toHaveLength(1);
    expect(modeCommands()[0]).toMatchObject({ type: "conversation.agent_mode.set", conversation_id: "plan-owner", agent_mode: "build" });
    expect(useAppStore.getState().promptDrafts[requestKey]?.planImplementationSettled).not.toBe(true);
    await reply(modeCommands()[0]);
    await waitFor(() => expect(useAppStore.getState().promptDrafts[requestKey].planImplementationSettled).toBe(true));
    expect(userCommands()).toEqual([]);
    expect(useAppStore.getState()).toMatchObject({ draft: saved.draft, attachments: saved.attachments, quotedMessage: saved.quotedMessage });
  });

  it("keeps a rejected semantic admission visible and allows a single retry", async () => {
    renderPrompt(); chooseImplement();
    await waitFor(() => expect(userCommands()).toHaveLength(1));
    await reply(userCommands()[0], false);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("消息发送失败"));
    expect(modeCommands()).toEqual([]);
    expect(useAppStore.getState().agentMode).toBe("plan");
    expect(useAppStore.getState().draft).toBe("原主草稿");
    chooseImplement();
    await waitFor(() => expect(userCommands()).toHaveLength(2));
    expect(userCommands()[1].client_command_id).not.toBe(userCommands()[0].client_command_id);
    await reply(userCommands()[1]);
    await waitFor(() => expect(modeCommands()).toHaveLength(1));
    await reply(modeCommands()[0]);
    await waitFor(() => expect(screen.queryByText("执行这个计划吗？")).toBeNull());
    expect(useAppStore.getState().messages.filter(message => message.role === "user")).toHaveLength(1);
  });

  it("applies a late admission only to its hidden owner while preserving both drafts and attachments", async () => {
    const saved = useAppStore.getState(); renderPrompt(); chooseImplement();
    await waitFor(() => expect(userCommands()).toHaveLength(1));
    act(() => { useAppStore.getState().applyConversationSwitched({ conversationId: "other-owner" });
      useAppStore.setState({ draft: "另一会话的未发送草稿", attachments: [attachment("other-attachment")] }); });
    const other = useAppStore.getState();
    await reply(userCommands()[0]);
    await waitFor(() => expect(modeCommands()).toHaveLength(1));
    expect(modeCommands()[0]).toMatchObject({ conversation_id: "plan-owner", agent_mode: "build" });
    await reply(modeCommands()[0]);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "other-owner", agentMode: "review", draft: other.draft, attachments: other.attachments });
    expect(useAppStore.getState().conversationWorkbenchStates["plan-owner"]).toMatchObject({ agentMode: "build", draft: saved.draft,
      attachments: saved.attachments, quotedMessage: saved.quotedMessage });
  });

  it("does not replace a newer hidden-owner mode choice when the old admission completes", async () => {
    renderPrompt(); chooseImplement();
    await waitFor(() => expect(userCommands()).toHaveLength(1));
    act(() => useAppStore.getState().applyConversationSwitched({ conversationId: "other-owner" }));
    let setting!: Promise<boolean>;
    act(() => { setting = useAppStore.getState().setAgentMode("explore", "plan-owner"); });
    await reply(modeCommands()[0]);
    expect(await setting).toBe(true);
    await reply(userCommands()[0]);
    await waitFor(() => expect(useAppStore.getState().promptDrafts[requestKey].planImplementationSettled).toBe(true));
    expect(modeCommands()).toHaveLength(1);
    expect(useAppStore.getState().conversationWorkbenchStates["plan-owner"].agentMode).toBe("explore");
    expect(useAppStore.getState().agentMode).toBe("review");
  });

  it("keeps an Escape rejected by the mode endpoint retryable without sending a message", async () => {
    const view = renderPrompt();
    fireEvent.keyDown(view.container.querySelector("form")!, { key: "Escape" });
    await reply(modeCommands()[0], false);
    await waitFor(() => expect(useAppStore.getState().agentMode).toBe("plan"));
    expect(useAppStore.getState().promptDrafts[requestKey]?.planImplementationSettled).not.toBe(true);
    expect(screen.getByText("执行这个计划吗？")).toBeTruthy();
    fireEvent.keyDown(view.container.querySelector("form")!, { key: "Escape" });
    expect(modeCommands()).toHaveLength(2);
    await reply(modeCommands()[1]);
    await waitFor(() => expect(screen.queryByText("执行这个计划吗？")).toBeNull());
    expect(userCommands()).toEqual([]);
    expect(useAppStore.getState().draft).toBe("原主草稿");
    expect(useAppStore.getState().attachments[0].id).toBe("saved-attachment");
  });

  it("does not overwrite a newer choice that changed away and back to Plan during admission", async () => {
    renderPrompt(); chooseImplement();
    await waitFor(() => expect(userCommands()).toHaveLength(1));
    let change!: Promise<boolean>;
    act(() => { change = useAppStore.getState().setAgentMode("build", "plan-owner"); });
    await reply(modeCommands()[0]);
    expect(await change).toBe(true);
    act(() => { change = useAppStore.getState().setAgentMode("plan", "plan-owner"); });
    await reply(modeCommands()[1]);
    expect(await change).toBe(true);
    await reply(userCommands()[0]);
    await waitFor(() => expect(useAppStore.getState().promptDrafts[requestKey].planImplementationSettled).toBe(true));
    expect(modeCommands()).toHaveLength(2);
    expect(useAppStore.getState().agentMode).toBe("plan");
  });
});
