/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { tmpdir } from "node:os";
import { useAppStore } from "../stores";
import { emptyComposerDraft } from "../stores/composer-drafts";
import { persistPromptDrafts, promptDraftKey } from "../stores/prompt-drafts";
import type { ClientCommand } from "../protocol/events";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { registerWebSocketSender, rejectAllPendingCommandResults } from "../protocol/ws-outbox";
import { handleControlEvent } from "./controlEvents";
import { handleRuntimeEvent } from "./runtimeEvents";
import { handleSessionEvent } from "./sessionEvents";
import { handleCommandResultEvent } from "./commandResultEvents";
import { InlineAgentPrompt } from "./InlineAgentPrompt";
import { Composer } from "../composer/Composer";
import { richComposerSchema as schema, richDocumentState } from "../composer/rich-composer-document";
import type { InlineComposerReference } from "../composer/composer-references";

const mocks = vi.hoisted(() => ({ send: vi.fn(async (_options: Record<string, unknown>) => true), toast: vi.fn() }));
vi.mock("./sendChatMessage", async original => ({ ...(await original<Record<string, unknown>>()), sendChatMessage: mocks.send }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../composer/contextPayload", () => ({ buildContextPayload: async () => "",
  buildContextNativeAttachments: async () => ({ attachments: [], attachmentRefs: [], notes: "" }) }));
vi.mock("../composer/ProjectScopePicker", () => ({ ProjectScopePicker: () => null }));
vi.mock("../composer/MenuOverlay", () => ({ MenuOverlay: () => null }));
vi.mock("../composer/FooterRow", () => ({ FooterRow: ({ onSend }: { onSend: () => void }) => <button onClick={onSend}>Send user draft</button> }));

type Scope = ReturnType<typeof scope>;
type PromptKind = "computer" | "form" | "question" | "url";
const scope = (conversationId = "A", suffix = "one") => ({ conversationId, turnId: `turn-${suffix}`,
  messageId: `message-${suffix}`, taskId: `task-${suffix}` });
const wireOwner = (owner: Scope) => ({ conversation_id: owner.conversationId, turn_id: owner.turnId,
  message_id: owner.messageId, task_id: owner.taskId });
const draftOwner = (owner: Scope, requestId = "shared-request") => ({ conversationId: owner.conversationId, requestId,
  inputOwner: { turnId: owner.turnId, messageId: owner.messageId, taskId: owner.taskId } });
const workspace = (owner: string) => `${tmpdir().replaceAll("\\", "/")}/approval-workspace/${owner}`;
const buffers = { textStreamBuffer: { push() {}, flush() {}, destroy() {} }, thinkingStreamBuffer: { push() {}, flush() {}, destroy() {} } };
const outgoing: ClientCommand[] = [];
const initial = useAppStore.getState();

function receive(raw: unknown) {
  const event = normalizeInboundServerEvent(raw);
  expect(event, `accepted wire ${(raw as { type: string }).type}`).not.toBeNull();
  act(() => {
    if (event!.type === "command.result") handleCommandResultEvent(event!);
    else if (event!.type === "conversation.switched") handleSessionEvent(event!, buffers);
    else if (event!.type === "subagent.plan_approval_requested") handleRuntimeEvent(event!, event!.conversation_id);
    else handleControlEvent(event!);
  });
}
const controls = () => outgoing.filter(command => command.type === "control_response" || command.type === "control_cancel_request");
async function settle(command: ClientCommand, success = true, message = "") {
  await act(async () => {
    receive({ type: "command.result", command: command.type, client_command_id: command.client_command_id,
      level: success ? "success" : "error", message, data: {} });
    await Promise.resolve();
  });
}
function ask(kind: PromptKind, owner = scope(), requestId = "shared-request") {
  const request = kind === "computer" ? { subtype: "elicitation", tool_use_id: requestId, mode: "form", mcp_server_name: "cua_repl", schema: {},
    question: "Allow access to the fixture-owned app?", prompt: "Allow access to the fixture-owned app?", _meta: {
      codex_approval_kind: "mcp_tool_call", connector_id: "computer-use", connector_name: "Computer Use", persist: ["session", "always"], riskLevel: "low",
      tool_params: { app: "Fixture.App" }, tool_params_display: [{ name: "app", display_name: "App", value: "Fixture app" }],
    } } : kind === "form" ? { subtype: "elicitation", tool_use_id: requestId, mode: "form", mcp_server_name: "fixture",
    question: "Choose the original scope", prompt: "Choose the original scope", schema: { type: "object", properties: { details: { type: "string", title: "Scope" } }, required: ["details"] } }
    : kind === "question" ? { subtype: "request_user_input", tool_use_id: requestId, isBlocking: true, questions: [
      { id: "scope", header: "Scope", question: "Which scope?", isOther: true, isSecret: false, options: [
        { label: "Current", description: "Keep the current scope." }, { label: "Other", description: "Use another scope." },
      ] },
    ] } : { subtype: "elicitation", tool_use_id: requestId, mode: "url", mcp_server_name: "fixture", question: "Complete the original action", prompt: "Complete the original action",
      url: "https://example.invalid/action", elicitation_id: "fixture-action" };
  receive({ type: "control_request", request_id: requestId, ...wireOwner(owner), replayed: true, request });
}
const cancel = (owner = scope(), requestId = "shared-request") => receive({ type: "approval.cancelled", ...wireOwner(owner),
  request_ids: [requestId], reason: "The original turn was cancelled" });
function submitPrompt(kind: PromptKind, container: HTMLElement) {
  if (kind === "computer") fireEvent.click(screen.getByRole("button", { name: /允许此对话/ }));
  else if (kind === "url") fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
  else if (kind === "form") {
    fireEvent.change(screen.getByRole("textbox", { name: "Scope" }), { target: { value: "Owned answer" } });
    fireEvent.submit(container.querySelector(".inline-prompt-question-card")!);
  } else {
    const input = screen.getByRole("textbox", { name: "回答当前问题" });
    fireEvent.focus(input); fireEvent.change(input, { target: { value: "Owned answer" } }); fireEvent.keyDown(input, { key: "Enter" });
  }
}
const busySurface = (container: HTMLElement) => container.querySelector('[aria-busy="true"]');
function switchTo(owner: string) {
  receive({ type: "conversation.switched", conversation_id: owner, is_hydrating: false,
    conversation: { id: owner, title: owner, updated_at: "2026-10-31T00:00:00Z", workspace_root: workspace(owner), messages: [] } });
  expect(useAppStore.getState().conversationId).toBe(owner);
}

beforeEach(() => {
  localStorage.clear(); mocks.send.mockClear(); mocks.toast.mockClear(); outgoing.length = 0;
  registerWebSocketSender(command => { outgoing.push(command); return true; });
  const rect = new DOMRect(0, 0, 600, 80);
  Object.defineProperty(Range.prototype, "getClientRects", { configurable: true, value: () => [rect] });
  Object.defineProperty(Range.prototype, "getBoundingClientRect", { configurable: true, value: () => rect });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(rect);
  vi.spyOn(window, "scrollBy").mockImplementation(() => {});
  useAppStore.setState({ ...initial, ...emptyComposerDraft(), composerDraftsReady: true, composerPlainTextMode: false,
    conversationId: "A", activeConversationDraftId: null, conversationDrafts: {}, homeConversationDraftIds: {}, workingDirectory: workspace("A"),
    conversations: ["A", "B"].map(id => ({ id, title: id, updatedAt: "2026-10-31", workspaceRoot: workspace(id) })),
    appMode: "cowork", isConnected: true, isStreaming: false, currentModel: "fixture-model", runtimeSession: null,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], diffReview: null,
    pendingConversationSwitchId: null, pendingConversationCreateId: null, promptDrafts: persistPromptDrafts({}), messages: [], conversationMessages: {},
    conversationStreaming: {}, conversationAgentStates: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {},
    sideChats: {}, slashPanelOpen: false, mentionPanelOpen: false, skillPanelOpen: false, slashCommands: [], activeGoal: null,
    selectedSkills: [], selectedMentions: [], availableSkills: [], gitChanges: { workingTree: [], staged: [], untracked: [], loading: false } });
});
afterEach(async () => { cleanup(); rejectAllPendingCommandResults("fixture cleanup"); await Promise.resolve(); registerWebSocketSender(null); vi.restoreAllMocks(); });

it.each<PromptKind>(["computer", "form", "question", "url"])("keeps %s in flight after switching away and back, through the real semantic result registry", async kind => {
  ask(kind); const view = render(<InlineAgentPrompt />); submitPrompt(kind, view.container);
  const command = controls()[0]; expect(command).toMatchObject(wireOwner(scope()));
  expect(useAppStore.getState().pendingAskUser?.status).toBe("submitted");
  act(() => useAppStore.setState({ conversationId: "B" }));
  ask(kind, scope("B", "two"));
  expect(busySurface(view.container)).toBeNull();
  act(() => useAppStore.setState({ conversationId: "A" }));
  expect(busySurface(view.container)).not.toBeNull();
  for (const button of view.container.querySelectorAll("button")) expect(button.disabled).toBe(true);
  expect(controls()).toHaveLength(1);
  await settle(command);
  expect(useAppStore.getState().pendingAskUser?.conversationId).toBe("B");
});

it.each<PromptKind>(["computer", "form", "question", "url"])("retains a late %s refusal on the original owner without showing it on another conversation", async kind => {
  ask(kind); const view = render(<InlineAgentPrompt />); submitPrompt(kind, view.container);
  const command = controls()[0];
  act(() => useAppStore.setState({ conversationId: "B" })); ask(kind, scope("B", "two"));
  await settle(command, false, "Original A response was refused");
  expect(screen.queryByText("Original A response was refused")).toBeNull();
  expect(mocks.toast.mock.calls.some(([message]) => message === "Original A response was refused")).toBe(false);
  expect(busySurface(view.container)).toBeNull();
  act(() => useAppStore.setState({ conversationId: "A" }));
  expect(screen.getByText("Original A response was refused")).toBeTruthy();
  expect(useAppStore.getState().pendingAskUser).toMatchObject({ conversationId: "A", status: "error", error: "Original A response was refused" });
  expect(busySurface(view.container)).toBeNull();
});

it.each<PromptKind>(["computer", "form", "question", "url"])("does not resurrect a cancelled %s or apply its late result to a same-id newer turn", async kind => {
  ask(kind); const view = render(<InlineAgentPrompt />); submitPrompt(kind, view.container);
  const command = controls()[0]; cancel(); ask(kind, scope("A", "two"));
  await settle(command, false, "Retired turn failure");
  expect(useAppStore.getState().pendingAskUser).toMatchObject({ turnId: "turn-two", messageId: "message-two", taskId: "task-two" });
  expect(useAppStore.getState().pendingAskUser?.status).toBeUndefined();
  expect(screen.queryByText("Retired turn failure")).toBeNull();
  expect(busySurface(view.container)).toBeNull();
});

it("keeps same-id form drafts separate by turn/task and leaves legacy unowned records untouched", () => {
  const legacy = { requestId: "shared-request", conversationId: "A", elicitationValues: { details: "Legacy text" } };
  const key = promptDraftKey(legacy);
  useAppStore.setState({ promptDrafts: persistPromptDrafts({ [key]: legacy }) });
  ask("form"); render(<InlineAgentPrompt />);
  expect(screen.getByRole("textbox", { name: "Scope" })).toHaveProperty("value", "");
  fireEvent.change(screen.getByRole("textbox", { name: "Scope" }), { target: { value: "First turn text" } });
  ask("form", scope("A", "two"));
  act(() => useAppStore.getState().updatePromptDraft(draftOwner(scope("A", "two")), { elicitationValues: { details: "Second turn text" } }));
  cancel();
  expect(screen.getByRole("textbox", { name: "Scope" })).toHaveProperty("value", "Second turn text");
  expect(useAppStore.getState().promptDrafts[key]).toEqual(legacy);
  expect(useAppStore.getState().promptDrafts[promptDraftKey(draftOwner(scope()))]).toBeUndefined();
});

it("keeps same-id plan approval edits scoped to their original request and removes only the settled draft", () => {
  const plan = "## Canonical plan\n\nImplement the requested change.";
  const planRequest = (owner: Scope) => receive({ type: "control_request", request_id: "same-plan", ...wireOwner(owner), replayed: true,
    request: { subtype: "can_use_tool", tool_use_id: "same-plan", tool_name: "exit_plan_mode", input: { plan } } });
  const legacy = { requestId: "same-plan", conversationId: "A", initialPlan: plan, plan: "Legacy plan", editing: true };
  const key = promptDraftKey(legacy); useAppStore.setState({ promptDrafts: persistPromptDrafts({ [key]: legacy }) });
  planRequest(scope()); render(<InlineAgentPrompt />);
  expect(screen.queryByText("Legacy plan")).toBeNull();
  act(() => useAppStore.getState().updatePromptDraft(draftOwner(scope(), "same-plan"), { initialPlan: plan, plan: "First owned edit", editing: true }));
  planRequest(scope("A", "two"));
  act(() => useAppStore.getState().updatePromptDraft(draftOwner(scope("A", "two"), "same-plan"), { initialPlan: plan, plan: "Second owned edit", editing: true }));
  cancel(scope(), "same-plan");
  expect(screen.getByRole("textbox", { name: "编辑计划" })).toHaveProperty("value", "Second owned edit");
  expect(useAppStore.getState().promptDrafts[key]).toEqual(legacy);
  expect(useAppStore.getState().promptDrafts[promptDraftKey(draftOwner(scope(), "same-plan"))]).toBeUndefined();
});

it("keeps an old two-part record distinct from an explicitly scoped draft even when optional owner fields are absent", () => {
  const legacy = { requestId: "ownerless-plan", conversationId: "A", plan: "Keep the old record" };
  const legacyKey = promptDraftKey(legacy);
  const scoped = { requestId: legacy.requestId, conversationId: "A", inputOwner: {} };
  useAppStore.setState({ promptDrafts: persistPromptDrafts({ [legacyKey]: legacy }) });
  useAppStore.getState().setApproval({ requestId: legacy.requestId, conversationId: "A", toolName: "ExitPlanMode", args: {} });
  useAppStore.getState().updatePromptDraft(scoped, { plan: "The current explicit owner" });
  useAppStore.getState().clearApproval(legacy.requestId, { conversationId: "A" });
  expect(useAppStore.getState().promptDrafts[legacyKey]).toEqual(legacy);
  expect(useAppStore.getState().promptDrafts[promptDraftKey(scoped)]).toBeUndefined();
});

const references = (): InlineComposerReference[] => [
  { kind: "plugin", name: "Computer Use", configName: "computer-use@runtime", path: "plugin://computer-use@runtime", pluginPath: `${workspace("A")}/plugins/computer-use`, iconVariant: "composer" },
  { kind: "skill", name: "workflow", path: `${workspace("A")}/skills/workflow/SKILL.md` },
  { kind: "url", name: "Original related chat", path: "codex://threads/related-chat" },
  { kind: "file", name: "main.ts", path: `${workspace("A")}/main.ts` },
  { kind: "folder", name: "src", path: `${workspace("A")}/src` },
];
function seedRichDraft() {
  const refs = references();
  const document = { ...richDocumentState(schema.node("doc", null, [
    schema.node("heading", { level: 2 }, schema.text("Owned draft")),
    schema.node("paragraph", null, [...refs.flatMap(reference => [schema.nodes.context_mention.create({ reference }), schema.text(" ")]),
      schema.text("Keep rich text", [schema.marks.strong.create()])]),
  ])), selection: { start: 3, end: 8 } };
  const attachment = { id: "owned-file", name: "notes.txt", type: "text/plain", size: 4, status: "ready" as const, conversationId: "A",
    artifactId: "owned-artifact", attachment: { id: "owned-artifact", artifact_id: "owned-artifact", file_name: "notes.txt", media_type: "text/plain", kind: "document" } };
  useAppStore.setState({ draft: document.markdown, richDraft: document, attachments: [attachment],
    selectedSkills: refs.filter((entry): entry is Extract<InlineComposerReference, { kind: "skill" }> => entry.kind === "skill"),
    selectedMentions: refs.filter((entry): entry is Exclude<InlineComposerReference, { kind: "skill" }> => entry.kind !== "skill") });
  return attachment;
}
const richInput = (container: HTMLElement) => container.querySelector<HTMLElement>("[data-rich-composer]")!;
const pasteText = (input: HTMLElement, text: string) => fireEvent.paste(input, { clipboardData: {
  items: [], files: [], types: ["text/plain"], getData: (type: string) => type === "text/plain" ? text : "",
} });

it.each(["accept", "decline", "cancel"])("restores the same real rich editor, five reference kinds and attachment after %s, then accepts new user input", async outcome => {
  const attachment = seedRichDraft(); const view = render(<Composer />); const input = richInput(view.container);
  const originalDocument = useAppStore.getState().richDraft!.document;
  const originalSelection = useAppStore.getState().richDraft!.selection;
  expect(input.querySelectorAll(".composer-inline-reference")).toHaveLength(5);
  receive({ type: "control_request", request_id: "shared-request", ...wireOwner(scope()), replayed: true,
    request: { subtype: "elicitation", tool_use_id: "shared-request", mode: "form", mcp_server_name: "cua_repl", schema: {},
      question: "Allow fixture app?", prompt: "Allow fixture app?", _meta: { codex_approval_kind: "mcp_tool_call", connector_id: "computer-use", connector_name: "Computer Use",
        persist: ["session"], riskLevel: "low", tool_params: { app: "Fixture.App" }, tool_params_display: [{ name: "app", display_name: "App", value: "Fixture app" }] } } });
  expect(input.closest(".composer-input-region")).toHaveProperty("hidden", true);
  expect(richInput(view.container)).toBe(input);
  if (outcome === "cancel") cancel();
  else {
    fireEvent.click(screen.getByRole("button", { name: outcome === "accept" ? /允许此对话/ : /^拒绝/ }));
    await settle(controls()[0]);
  }
  expect(input.closest(".composer-input-region")).toHaveProperty("hidden", false);
  expect(richInput(view.container)).toBe(input);
  expect(useAppStore.getState().richDraft!.document).toEqual(originalDocument);
  expect(useAppStore.getState().richDraft!.selection).toEqual(originalSelection);
  expect(useAppStore.getState().attachments).toEqual([attachment]);
  expect(input.querySelectorAll(".composer-inline-reference")).toHaveLength(5);
  pasteText(input, " New input");
  expect(useAppStore.getState().draft).toContain("New input");
  fireEvent.click(screen.getByRole("button", { name: "Send user draft" }));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(mocks.send).toHaveBeenCalledOnce();
  expect(mocks.send.mock.calls[0][0]).toMatchObject({ conversationId: "A" });
});

it("restores each real owner's rich draft across accepted conversation switches while an old approval response arrives", async () => {
  const attachment = seedRichDraft(); const view = render(<Composer />); const originalDocument = useAppStore.getState().richDraft!.document;
  const originalSelection = useAppStore.getState().richDraft!.selection;
  ask("computer"); fireEvent.click(screen.getByRole("button", { name: /允许此对话/ })); const command = controls()[0];
  switchTo("B");
  const other = richInput(view.container);
  pasteText(other, "B new draft");
  ask("computer", scope("B", "two")); switchTo("A");
  expect(busySurface(view.container)).not.toBeNull();
  expect(useAppStore.getState().richDraft!.document).toEqual(originalDocument);
  expect(useAppStore.getState().richDraft!.selection).toEqual(originalSelection);
  expect(useAppStore.getState().attachments).toEqual([attachment]);
  switchTo("B"); await settle(command, false, "A response refused while hidden");
  expect(useAppStore.getState().draft).toBe("B new draft");
  expect(screen.queryByText("A response refused while hidden")).toBeNull();
  switchTo("A"); expect(screen.getByText("A response refused while hidden")).toBeTruthy();
  cancel(); expect(richInput(view.container).closest(".composer-input-region")).toHaveProperty("hidden", false);
  expect(richInput(view.container).querySelectorAll(".composer-inline-reference")).toHaveLength(5);
});

it("replaces the composer for an actual teammate plan approval and preserves its draft on cancellation", () => {
  seedRichDraft(); const view = render(<Composer />); const input = richInput(view.container);
  receive({ type: "subagent.plan_approval_requested", conversation_id: "A", subagent_id: "teammate", request_id: "team-plan",
    teammate_name: "Builder", team_name: "release", plan_file_path: `${workspace("A")}/plan.md`, plan_content: "## Implement the requested change" });
  expect(input.closest(".composer-input-region")).toHaveProperty("hidden", true);
  receive({ type: "approval.cancelled", conversation_id: "A", request_ids: ["team-plan"], reason: "Leader cancelled the plan" });
  expect(input.closest(".composer-input-region")).toHaveProperty("hidden", false);
  expect(input.querySelectorAll(".composer-inline-reference")).toHaveLength(5);
});

it("keeps a teammate plan response attached to its original conversation across remount and semantic refusal", async () => {
  const request = (conversationId: string) => receive({ type: "subagent.plan_approval_requested", conversation_id: conversationId,
    subagent_id: `teammate-${conversationId}`, request_id: "team-plan", teammate_name: `Builder ${conversationId}`,
    team_name: "release", plan_file_path: `${workspace(conversationId)}/plan.md`, plan_content: "## Implement the requested change" });
  request("A"); const view = render(<InlineAgentPrompt />);
  fireEvent.click(screen.getByRole("button", { name: "批准子智能体的计划" }));
  const command = outgoing.find(item => item.type === "subagent.plan_review")!;
  expect(command).toMatchObject({ conversation_id: "A", subagent_id: "teammate-A" });
  act(() => useAppStore.setState({ conversationId: "B" })); request("B");
  act(() => useAppStore.setState({ conversationId: "A" }));
  expect(busySurface(view.container)).not.toBeNull();
  act(() => useAppStore.setState({ conversationId: "B" }));
  await settle(command, false, "Original teammate plan response refused");
  expect(screen.queryByText("Original teammate plan response refused")).toBeNull();
  act(() => useAppStore.setState({ conversationId: "A" }));
  expect(screen.getByText("Original teammate plan response refused")).toBeTruthy();
  expect(busySurface(view.container)).toBeNull();
});
