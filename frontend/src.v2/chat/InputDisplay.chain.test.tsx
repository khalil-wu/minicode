// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerEvent, UserMessageCommand } from "../protocol/events";
import type { ChatMessage, ComposerQuote, MessageContextRef } from "../stores/types";
import { useAppStore } from "../stores";
import { resetSendDeduplication, sendChatMessage } from "./sendChatMessage";
import { hydrateMessages } from "./transcriptHydration";
import { handleSessionEvent } from "./sessionEvents";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { UserMessageCell } from "./cells/UserMessageCell";
import { AssistantMarkdownCell } from "./cells/AssistantMarkdownCell";

const mocks = vi.hoisted(() => ({ send: vi.fn(() => true), receipt: vi.fn(), toast: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "input-session", send: mocks.send }) }));
vi.mock("../protocol/ws-outbox", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.send, sendClientCommandAwaitResult: mocks.receipt,
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../overlays/DialogService", () => ({ showConfirm: vi.fn(async () => true) }));
vi.mock("./messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <p>{content}</p> }));

const initial = useAppStore.getState();
const refs: MessageContextRef[] = [
  { kind: "file", name: "main.ts", path: "C:/A/main.ts" },
  { kind: "folder", name: "src", path: "C:/A/src" },
  { kind: "url", name: "reference", path: "https://example.test/reference" },
  { kind: "skill", name: "workflow", path: "C:/A/.minicode/skills/workflow/SKILL.md", description: "selected skill", sourceLevel: "project" },
  { kind: "plugin", name: "docs", configName: "docs", path: "plugin://docs", description: "selected plugin" },
  { kind: "browser_annotation", name: "target", path: "https://example.test/page", url: "https://example.test/page",
    note: "this exact target", selector: "#main", targetId: "tab-A", xPercent: 0, yPercent: 0.5,
    widthPercent: 0.25, heightPercent: 0.2, viewportWidth: 1200, viewportHeight: 800 },
];
const quote: ComposerQuote = { id: "quoted-answer", role: "assistant", content: "exact previous reply\nsecond line" };
const input = "Question @main.ts";
const modelInput = "File reference: C:/A/main.ts\n\nQuoted Assistant message:\n" + quote.content + "\n\n" + input;
const attachment = { id: "text", artifact_id: "artifact-A", kind: "document", file_name: "pasted.txt",
  media_type: "text/plain", size_bytes: 12, input_source: "pasted_text", source_char_count: 12 };
const buffers = () => ({
  textStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() },
  thinkingStreamBuffer: { push: vi.fn(), flush: vi.fn(), destroy: vi.fn() },
});
const receipt = () => ({ type: "command.result", command: "conversation.truncate", level: "success", message: "" });
const persistedInput = (display = input) => ({
  id: "input-user", role: "user", content: modelInput, display_content: display,
  context_refs: refs, quoted_message: quote, attachments: [attachment], timestamp: 1,
});
const synced = (session: Record<string, unknown>) => handleSessionEvent({
  type: "session.synced", protocol_version: "1.0.0", active_conversation_id: "A",
  session: { session_id: "input-session", active_conversation_id: "A", workspace_root: "C:/A", ...session },
} as ServerEvent, buffers());

beforeEach(() => {
  vi.clearAllMocks();
  resetSendDeduplication();
  mocks.receipt.mockResolvedValue(receipt());
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A",
    conversations: [{ id: "A", title: "A", updatedAt: "", workspaceRoot: "C:/A" }],
    messages: [], conversationMessages: {}, conversationStreaming: {}, conversationRecallTruncations: {},
    conversationHistoryPages: {}, conversationAgentStates: {}, conversationWorkbenchStates: {},
    isConnected: true, serverReady: true, isStreaming: false, runtimeSession: null,
    draft: "", attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null, sideChats: {} }, true);
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("user input, model context and durable display", () => {
  it("sends the exact display/ref/quote envelope while preserving assembled model input", () => {
    expect(sendChatMessage({ displayContent: input, backendContent: modelInput, conversationId: "A",
      contextRefs: refs, quotedMessage: quote, attachments: [attachment] })).toBe(true);
    const command = mocks.send.mock.calls.find(([value]) => value.type === "user_message")![0] as UserMessageCommand;
    expect(command).toMatchObject({ content: modelInput, display_content: input, context_refs: refs, quoted_message: quote });
    const local = useAppStore.getState().messages[0];
    expect(local).toMatchObject({ content: input, backendContent: modelInput, contextRefs: refs, quotedMessage: quote });
    const [restored] = hydrateMessages([{ ...persistedInput(), id: command.user_message_id }]);
    expect(restored).toMatchObject({ content: input, backendContent: modelInput, contextRefs: refs, quotedMessage: quote });
    expect(restored.attachmentRefs?.[0]).toMatchObject({ inputSource: "pasted_text", sourceCharCount: 12 });
  });

  it("keeps an explicit empty display for reference-only input", () => {
    expect(sendChatMessage({ displayContent: "", backendContent: "File reference: C:/A/main.ts",
      contextRefs: [refs[0]], conversationId: "A" })).toBe(true);
    expect(useAppStore.getState().messages[0].content).toBe("");
    const [restored] = hydrateMessages([persistedInput("")]);
    expect(restored.content).toBe("");
    const turn = projectMessagesToTurns([restored], false)[0];
    render(<UserMessageCell cell={turn.userCell!} conversationId="A" isTranscriptMode />);
    expect(screen.getByLabelText("消息上下文").textContent).toContain("@main.ts");
    expect(screen.queryByText(modelInput)).toBeNull();
    expect(screen.queryByRole("button", { name: "取消引用" })).toBeNull();
  });

  it("recalls literal authored text and restores all original refs and quote after authoritative truncation", async () => {
    const messages = hydrateMessages([persistedInput()]);
    useAppStore.setState({ messages, conversationMessages: { A: messages } });
    mocks.receipt.mockImplementationOnce(async () => {
      useAppStore.getState().hydrateConversationMessages("A", [], { activate: true, isStreaming: false });
      return receipt();
    });
    expect(await useAppStore.getState().recallMessage("input-user")).toBe(true);
    const current = useAppStore.getState();
    expect(current.draft).toBe(input);
    expect(current.selectedMentions).toMatchObject(refs.filter((ref) => ref.kind !== "skill"));
    expect(current.selectedSkills).toMatchObject(refs.filter((ref) => ref.kind === "skill"));
    expect(current.quotedMessage).toEqual(quote);
  });

  it("leaves old stored text intact when the history has no original display metadata", async () => {
    const [old] = hydrateMessages([{ id: "old-input", role: "user", content: input, context_refs: [refs[0]] }]);
    expect(old.backendContent).toBeUndefined();
    useAppStore.setState({ messages: [old], conversationMessages: { A: [old] } });
    expect(await useAppStore.getState().recallMessage("old-input")).toBe(true);
    expect(useAppStore.getState().draft).toBe(input);
    expect(useAppStore.getState().quotedMessage).toBeNull();
  });

  it("regenerates the saved assembled input once while retaining the original display and quote", async () => {
    const user = hydrateMessages([persistedInput()])[0];
    const assistant: ChatMessage = { id: "assistant", role: "assistant", content: "owned answer", artifacts: [], timestamp: 2, terminalStatus: "completed" };
    useAppStore.setState({ messages: [user, assistant], conversationMessages: { A: [user, assistant] } });
    render(<AssistantMarkdownCell cell={{ kind: "assistant_markdown", id: "final", messageId: "assistant",
      markdownSource: "owned answer", phase: "final", copyable: false, createdAt: 2 }} conversationId="A" workspaceRoot="C:/A" />);
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({
      type: "user_message", content: modelInput, display_content: input, quoted_message: quote, retry_from_message_id: "input-user",
    })));
    const command = mocks.send.mock.calls.find(([value]) => value.type === "user_message")![0] as UserMessageCommand;
    expect(command.context_refs).toMatchObject(refs);
    expect(command.content.match(/File reference:/g)).toHaveLength(1);
  });
});

describe("queued and steer input restoration", () => {
  it("restores input metadata before its existing assistant placeholder without treating the assistant as a user", () => {
    const placeholder: ChatMessage = { id: "queued-assistant", role: "assistant", content: "", artifacts: [], timestamp: 1,
      queueState: "queued", queueMessageId: "queued-assistant" };
    useAppStore.setState({ messages: [placeholder], conversationMessages: {} });
    act(() => synced({ active_stream_conversation_ids: [], queued_user_messages: [{
      conversation_id: "A", message_id: "queued-assistant", user_message_id: "queued-user",
      content: modelInput, display_content: "", context_refs: refs, quoted_message: quote, attachments: [attachment],
    }] }));
    const current = useAppStore.getState().messages;
    expect(current.map((message) => [message.id, message.role])).toEqual([["queued-user", "user"], ["queued-assistant", "assistant"]]);
    expect(current[0]).toMatchObject({ content: "", backendContent: modelInput, contextRefs: refs, quotedMessage: quote });
    expect(current[0].attachmentRefs?.[0]).toMatchObject({ artifactId: "artifact-A", sourceCharCount: 12 });
  });

  it("restores a steered input with its display/ref/quote and original attachment metadata", () => {
    const assistant: ChatMessage = { id: "live-assistant", role: "assistant", content: "", blocks: [], artifacts: [], timestamp: 1, isStreaming: true };
    useAppStore.setState({ messages: [assistant], conversationMessages: { A: [assistant] }, isStreaming: true });
    act(() => synced({ active_stream_conversation_ids: ["A"], active_task_id: "run-A", pending_turn_inputs: [{
      mode: "steer", conversation_id: "A", message_id: "steer-placeholder", user_message_id: "steer-user",
      target_message_id: "live-assistant", content: modelInput, display_content: input,
      context_refs: refs, quoted_message: quote, attachments: [attachment],
    }] }));
    const current = useAppStore.getState().messages;
    expect(current[0]).toMatchObject({ id: "steer-user", role: "user", content: input, backendContent: modelInput,
      contextRefs: refs, quotedMessage: quote, steeredIntoMessageId: "live-assistant" });
    expect(current[0].attachmentRefs?.[0]).toMatchObject({ inputSource: "pasted_text", sourceCharCount: 12 });
    expect(current[1].id).toBe("live-assistant");
  });

  it("preserves a known local display when an old queued snapshot lacks display metadata", () => {
    const local = hydrateMessages([persistedInput()])[0];
    act(() => {
      useAppStore.setState({ messages: [local], conversationMessages: {} });
      synced({ active_stream_conversation_ids: [], queued_user_messages: [{
        conversation_id: "A", message_id: "queued-assistant", user_message_id: "input-user", content: modelInput,
      }] });
    });
    expect(useAppStore.getState().messages[0].content).toBe(input);
  });
});
