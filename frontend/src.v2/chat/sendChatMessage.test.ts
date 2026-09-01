import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetSendDeduplication, sendChatMessage } from "./sendChatMessage";
import { useAppStore } from "../stores";

const wsMock = vi.hoisted(() => ({
  sent: [] as unknown[],
  clientCommands: [] as unknown[],
  throwOnSend: false,
  acceptSend: true,
}));
const sent = wsMock.sent;
const clientCommands = wsMock.clientCommands;

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({
    send: (command: unknown) => {
      if (wsMock.throwOnSend) {
        throw new Error("socket closed");
      }
      if (!wsMock.acceptSend) return false;
      sent.push(command);
      return true;
    },
    sessionId: "session-test",
  }),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: (command: unknown) => {
    clientCommands.push(command);
    return true;
  },
  sendClientCommandAwaitResult: async (command: unknown, expectedCommand: string) => {
    clientCommands.push(command);
    return { type: "command.result", command: expectedCommand, level: "success", message: "", data: {} };
  },
  commandResultSucceeded: (event: { level?: string }) => !["error", "failed"].includes(String(event.level || "")),
}));

describe("sendChatMessage attachment feedback", () => {
  beforeEach(() => {
    resetSendDeduplication();
    sent.length = 0;
    clientCommands.length = 0;
    wsMock.throwOnSend = false;
    wsMock.acceptSend = true;
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      conversationRecallTruncations: {},
      isConnected: true,
      isStreaming: false,
      pendingApproval: null,
      pendingDiffReview: null,
      pendingAskUser: null,
      runtimeSession: null,
      agentMode: "build",
      activeTabPath: null,
      activeEditorPath: null,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps uploaded file refs on the local user message", () => {
    const ok = sendChatMessage({
      displayContent: "please inspect this",
      backendContent: "please inspect this",
      attachments: [{
        id: "att-1",
        file_name: "report.pdf",
        kind: "document",
        media_type: "application/pdf",
        artifact_id: "artifact-1",
        doc_id: "doc-1",
        size_bytes: 2048,
        data: "JVBERi0xLjQ=".repeat(10_000),
      }],
      attachmentRefs: [{
        id: "att-1",
        name: "report.pdf",
        kind: "document",
        mediaType: "application/pdf",
        artifactId: "artifact-1",
        docId: "doc-1",
        sizeBytes: 2048,
      }],
    });

    expect(ok).toBe(true);
    expect(sent).toHaveLength(1);
    const sentCommand = sent[0] as { attachments?: Record<string, unknown>[] };
    expect(sentCommand.attachments?.[0]).not.toHaveProperty("data");
    expect(JSON.stringify(sent[0]).length).toBeLessThan(2_000);
    expect(useAppStore.getState().messages[0]).toMatchObject({
      role: "user",
      content: "please inspect this",
      attachmentRefs: [{
        name: "report.pdf",
        artifactId: "artifact-1",
      }],
    });
  });

  it("does not deduplicate the same text when the selected plugin changes", () => {
    const first = sendChatMessage({
      displayContent: "check this",
      backendContent: "check this",
      contextRefs: [{
        kind: "plugin",
        name: "docs",
        configName: "docs",
        path: "plugin://docs",
      }],
      skipLocalAppend: true,
    });
    const second = sendChatMessage({
      displayContent: "check this",
      backendContent: "check this",
      contextRefs: [{
        kind: "plugin",
        name: "review",
        configName: "review",
        path: "plugin://review",
      }],
      skipLocalAppend: true,
    });

    expect(first).toBe(true);
    expect(second).toBe(true);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatchObject({
      plugins: [{ config_name: "docs", path: "plugin://docs" }],
    });
    expect(sent[1]).toMatchObject({
      plugins: [{ config_name: "review", path: "plugin://review" }],
    });
  });

  it("sends caller-owned ids for a side-chat placeholder", () => {
    const ok = sendChatMessage({
      displayContent: "side question",
      backendContent: "side question",
      conversationId: "side-1",
      skipLocalAppend: true,
      assistantMessageId: "m-side-assistant",
      userMessageId: "m-side-user",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      conversation_id: "side-1",
      assistant_message_id: "m-side-assistant",
      user_message_id: "m-side-user",
    });
  });

  it("keeps artifact identity but drops temporary blob URLs from sent image messages", () => {
    const ok = sendChatMessage({
      displayContent: "what is this?",
      attachments: [{
        id: "att-image",
        file_name: "image.png",
        kind: "image",
        media_type: "image/png",
        artifact_id: "artifact-image",
      }],
      attachmentRefs: [{
        id: "att-image",
        name: "image.png",
        kind: "image",
        mediaType: "image/png",
        sizeBytes: 1024,
        artifactId: "artifact-image",
        dataUrl: "blob:http://localhost/temporary-image",
      }],
    });

    expect(ok).toBe(true);
    expect(useAppStore.getState().messages[0]?.attachmentRefs).toEqual([
      expect.objectContaining({ artifactId: "artifact-image", name: "image.png" }),
    ]);
    expect(useAppStore.getState().messages[0]?.attachmentRefs?.[0]).not.toHaveProperty("dataUrl");
  });

  it("sends the local streaming assistant id so backend events bind to the same turn", () => {
    const ok = sendChatMessage({
      displayContent: "换个游戏吧 我要完cf",
      backendContent: "换个游戏吧 我要完cf",
    });

    expect(ok).toBe(true);
    const assistant = useAppStore.getState().messages.find((message) => message.role === "assistant");
    expect(assistant?.isStreaming).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "换个游戏吧 我要完cf",
      assistant_message_id: assistant?.id,
    });
  });

  it("sends attachment-only pasted text without inventing a separate prompt", () => {
    const attachment = {
      id: "artifact-paste",
      file_name: "pasted-3.txt",
      kind: "document",
      media_type: "text/plain",
      artifact_id: "artifact-paste",
      input_source: "pasted_text",
      source_char_count: 22_000,
    };

    const ok = sendChatMessage({
      displayContent: "",
      backendContent: "",
      attachments: [attachment],
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "",
      attachments: [attachment],
    });
    expect(useAppStore.getState().messages[0]).toMatchObject({
      role: "user",
      content: "",
      attachmentRefs: [{
        name: "pasted-3.txt",
        artifactId: "artifact-paste",
        inputSource: "pasted_text",
        sourceCharCount: 22_000,
      }],
    });
  });

  it("queues a follow-up without replacing the active turn state", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      isStreaming: true,
      conversationStreaming: { "conv-test": true },
      messages: [{
        id: "assistant-running",
        role: "assistant",
        content: "working",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      plan: {
        threadId: "conv-test",
        turnId: "turn-active",
        plan: [{ step: "Keep working", status: "in_progress" }],
      },
    });

    const ok = sendChatMessage({
      displayContent: "do this next",
      backendContent: "do this next",
      allowWhileStreaming: true,
    });

    const state = useAppStore.getState();
    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "do this next",
      queue_if_busy: true,
      streaming_behavior: "follow_up",
      user_message_id: expect.any(String),
      assistant_message_id: expect.any(String),
    });
    expect(state.plan?.turnId).toBe("turn-active");
    expect(state.messages.at(-2)).toMatchObject({ role: "user", queueState: "queued" });
    expect(state.messages.at(-1)).toMatchObject({ role: "assistant", queueState: "queued", isStreaming: false });
  });

  it("allows sending after the server confirms a freshly created conversation", async () => {
    useAppStore.setState({
      conversationId: "conv-old",
      conversations: [{ id: "conv-old", title: "Old", updatedAt: "2026-01-01T00:00:00.000Z" }],
      messages: [
        { id: "old-user", role: "user", content: "old", artifacts: [], timestamp: 1 },
        {
          id: "old-assistant",
          role: "assistant",
          content: "",
          blocks: [{ type: "thinking", content: "still working" }],
          artifacts: [],
          timestamp: 2,
          isStreaming: true,
          isThinkingStreaming: true,
        },
      ],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: true,
    });

    await useAppStore.getState().createConversation();
    const createCommand = clientCommands.find((command) => (command as { type?: string }).type === "conversation.create") as { conversation_id: string };
    const newConversationId = createCommand.conversation_id;
    useAppStore.setState((state) => ({
      conversations: [{ id: newConversationId, title: "New chat", updatedAt: "2026-01-01T00:00:01.000Z" }, ...state.conversations],
    }));
    useAppStore.getState().applyConversationSwitched({ conversationId: newConversationId });
    expect(newConversationId).toBeTruthy();
    expect(newConversationId).not.toBe("conv-old");
    expect(useAppStore.getState().isStreaming).toBe(false);
    expect(useAppStore.getState().conversationStreaming["conv-old"]).toBe(true);

    const ok = sendChatMessage({
      displayContent: "new task",
      backendContent: "new task",
    });

    expect(ok).toBe(true);
    expect(sent.at(-1)).toMatchObject({
      type: "user_message",
      conversation_id: newConversationId,
      content: "new task",
    });
    expect(useAppStore.getState().messages.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(useAppStore.getState().conversationStreaming["conv-old"]).toBe(true);
  });

  it("recalls uploaded file refs into the composer attachments", async () => {
    useAppStore.getState().sendMessage("check this", {
      assistant: false,
      attachmentRefs: [{
        id: "att-2",
        name: "diagram.png",
        kind: "image",
        mediaType: "image/png",
        artifactId: "artifact-2",
        docId: "doc-2",
        sizeBytes: 4096,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    await useAppStore.getState().recallMessage(userMessage.id);

    expect(useAppStore.getState().draft).toBe("check this");
    expect(useAppStore.getState().attachments[0]).toMatchObject({
      name: "diagram.png",
      type: "image/png",
      status: "ready",
      conversationId: "conv-test",
      artifactId: "artifact-2",
      attachment: {
        artifact_id: "artifact-2",
        file_name: "diagram.png",
      },
    });
    expect(useAppStore.getState().actionChip).toBeNull();
  });

  it("keeps the recalled attachment owner and durable handle on the second send", async () => {
    useAppStore.getState().sendMessage("first request", {
      assistant: false,
      attachmentRefs: [{
        id: "att-recall-resend",
        name: "requirements.txt",
        kind: "document",
        mediaType: "text/plain",
        artifactId: "artifact-recall-resend",
        docId: "doc-recall-resend",
        sizeBytes: 128,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    expect(await useAppStore.getState().recallMessage(userMessage.id)).toBe(true);
    const restored = useAppStore.getState().attachments[0];

    expect(restored).toMatchObject({
      status: "ready",
      conversationId: "conv-test",
      artifactId: "artifact-recall-resend",
    });
    expect(sendChatMessage({
      displayContent: "updated request",
      backendContent: "updated request",
      attachments: [restored.attachment!],
      attachmentRefs: [{
        id: "att-recall-resend",
        name: restored.name,
        kind: "document",
        mediaType: restored.type,
        artifactId: restored.artifactId,
        docId: restored.docId,
        sizeBytes: restored.size,
      }],
      conversationId: restored.conversationId,
    })).toBe(true);

    expect(sent.at(-1)).toMatchObject({
      type: "user_message",
      conversation_id: "conv-test",
      content: "updated request",
      attachments: [{
        artifact_id: "artifact-recall-resend",
        doc_id: "doc-recall-resend",
        file_name: "requirements.txt",
      }],
    });
  });

  it("does not mark a recalled attachment without a durable handle as ready", async () => {
    useAppStore.getState().sendMessage("legacy attachment", {
      assistant: false,
      attachmentRefs: [{
        id: "legacy-only-id",
        name: "legacy.txt",
        kind: "document",
        mediaType: "text/plain",
        sizeBytes: 12,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    expect(await useAppStore.getState().recallMessage(userMessage.id)).toBe(true);

    expect(useAppStore.getState().attachments[0]).toMatchObject({
      name: "legacy.txt",
      status: "error",
      conversationId: "conv-test",
      error: "原附件缺少可验证的持久化引用，请重新上传。",
    });
    expect(useAppStore.getState().attachments[0]).not.toHaveProperty("attachment");
  });

  it("recalls from a user message and removes the later transcript", async () => {
    useAppStore.setState({
      messages: [
        {
          id: "user-1",
          role: "user",
          content: "help me optimize this project",
          artifacts: [],
          timestamp: 1,
        },
        {
          id: "assistant-working",
          role: "assistant",
          content: "",
          blocks: [{ type: "thinking", content: "Choosing the next step" }],
          artifacts: [],
          timestamp: 2,
        },
        {
          id: "assistant-recall",
          role: "assistant",
          content: "Recalling previous edits",
          blocks: [
            {
              type: "tool_call",
              record: {
                id: "tool-1",
                name: "recall",
                status: "completed",
                input: { query: "project optimization" },
                output: "Found 3 prior changes",
              },
            },
            { type: "text", content: "Recalling previous edits" },
          ],
          artifacts: [],
          timestamp: 3,
        },
        {
          id: "assistant-final",
          role: "assistant",
          content: "I found the previous changes and can continue from there.",
          artifacts: [],
          timestamp: 4,
        },
        {
          id: "user-2",
          role: "user",
          content: "next turn",
          artifacts: [],
          timestamp: 5,
        },
      ],
      isStreaming: false,
      conversationId: "conv-test",
    });

    await useAppStore.getState().recallMessage("user-1");

    expect(useAppStore.getState().draft).toBe("help me optimize this project");
    expect(useAppStore.getState().messages.map((message) => message.id)).toEqual([]);
    expect(clientCommands).toContainEqual({
      type: "conversation.truncate",
      conversation_id: "conv-test",
      truncate_before_message_id: "user-1",
      retained_message_ids: [],
    });
    expect(clientCommands).toContainEqual({
      type: "session.usage.inspect",
      conversation_id: "conv-test",
      source: "conversation_recall",
      silent: true,
    });
  });

  it("does not resurrect recalled-away messages when the old transcript hydrates again", async () => {
    const transcript = [
      {
        id: "user-1",
        role: "user" as const,
        content: "help me optimize this project",
        artifacts: [],
        timestamp: 1,
      },
      {
        id: "assistant-old",
        role: "assistant" as const,
        content: "old branch answer",
        artifacts: [],
        timestamp: 2,
      },
      {
        id: "user-old-tail",
        role: "user" as const,
        content: "old tail",
        artifacts: [],
        timestamp: 3,
      },
    ];
    useAppStore.setState({
      conversationId: "conv-test",
      messages: transcript,
      conversationMessages: { "conv-test": transcript },
      conversationStreaming: { "conv-test": false },
      conversationRecallTruncations: {},
      isStreaming: false,
    });

    await useAppStore.getState().recallMessage("user-1");
    useAppStore.getState().hydrateConversationMessages("conv-test", transcript, { activate: true, isStreaming: false });

    const state = useAppStore.getState();
    expect(state.messages).toEqual([]);
    expect(state.conversationMessages["conv-test"]).toEqual([]);
    expect(state.conversationRecallTruncations["conv-test"].removedIds).toEqual([
      "user-1",
      "assistant-old",
      "user-old-tail",
    ]);
  });

  it("recalls an assistant reply by returning to its user turn", async () => {
    useAppStore.setState({
      messages: [
        {
          id: "user-1",
          role: "user",
          content: "first turn",
          artifacts: [],
          timestamp: 1,
        },
        {
          id: "assistant-1",
          role: "assistant",
          content: "first answer",
          artifacts: [],
          timestamp: 2,
        },
        {
          id: "user-2",
          role: "user",
          content: "second turn",
          artifacts: [],
          timestamp: 3,
        },
        {
          id: "assistant-2",
          role: "assistant",
          content: "second answer",
          artifacts: [],
          timestamp: 4,
        },
      ],
      isStreaming: false,
      conversationId: "conv-test",
    });

    await useAppStore.getState().recallMessage("assistant-1");

    expect(useAppStore.getState().draft).toBe("first turn");
    expect(useAppStore.getState().messages.map((message) => message.id)).toEqual([]);
  });

  it("promotes a busy follow-up into the active turn when steer is selected", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      isStreaming: true,
      conversationStreaming: { "conv-test": true },
      messages: [{
        id: "assistant-running",
        role: "assistant",
        content: "working",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
    });

    expect(sendChatMessage({
      displayContent: "change direction",
      backendContent: "change direction",
      allowWhileStreaming: true,
      busyBehavior: "steer",
    })).toBe(true);

    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      queue_if_busy: true,
      streaming_behavior: "steer",
    });
  });

  it("restores structured plugin and browser context when recalling a user turn", async () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "user-context",
        role: "user",
        content: "inspect this page @Docs @Login button",
        contextRefs: [
          { kind: "plugin", name: "Docs", configName: "docs", path: "plugin://docs" },
          {
            kind: "browser_annotation",
            name: "Login button",
            path: "https://example.test/login",
            url: "https://example.test/login",
            note: "Button does nothing",
          },
          { kind: "skill", name: "browser-debug", path: "C:/skills/browser-debug/SKILL.md" },
        ],
        artifacts: [],
        timestamp: 1,
      }],
      selectedMentions: [],
      selectedSkills: [],
      isStreaming: false,
    });

    await useAppStore.getState().recallMessage("user-context");

    expect(useAppStore.getState().selectedMentions).toEqual([
      { kind: "plugin", name: "Docs", configName: "docs", path: "plugin://docs" },
      {
        kind: "browser_annotation",
        name: "Login button",
        path: "https://example.test/login",
        url: "https://example.test/login",
        note: "Button does nothing",
      },
    ]);
    expect(useAppStore.getState().selectedSkills).toEqual([
      { kind: "skill", name: "browser-debug", path: "C:/skills/browser-debug/SKILL.md" },
    ]);
    expect(useAppStore.getState().draft).toBe("inspect this page");
  });

  it("resumes an assistant message and seals it when streaming finishes", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
    });

    useAppStore.getState().resumeStreaming("conv-test", [
      { id: "tool-1", name: "recall", args: { query: "previous edits" } },
    ]);

    let resumed = useAppStore.getState().messages[0];
    expect(resumed?.isStreaming).toBe(true);

    useAppStore.getState().finishStreaming("conv-test");

    resumed = useAppStore.getState().messages[0];
    expect(resumed?.isStreaming).toBe(false);
    expect(resumed?.completedAt).toEqual(expect.any(Number));
  });

  it("tracks streaming state per conversation instead of only on the active thread", () => {
    useAppStore.setState({
      conversationId: "conv-a",
      messages: [],
      conversationMessages: {
        "conv-b": [{
          id: "assistant-b",
          role: "assistant",
          content: "",
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        }],
      },
      conversationStreaming: {
        "conv-b": true,
      },
      isStreaming: false,
    });

    expect(useAppStore.getState().conversationStreaming["conv-b"]).toBe(true);
    expect(useAppStore.getState().conversationMessages["conv-b"]?.[0]?.isStreaming).toBe(true);
    expect(useAppStore.getState().isStreaming).toBe(false);
  });

  it("allows sending to an idle side conversation while the active conversation is streaming", () => {
    useAppStore.setState({
      conversationId: "conv-main",
      messages: [{
        id: "assistant-main",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      sideChats: {
        "side-1": {
          id: "side-1",
          messages: [],
          draft: "",
          isStreaming: false,
          inheritedContext: "",
        },
      },
      conversationMessages: {},
      conversationStreaming: { "conv-main": true },
      isStreaming: true,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "side question",
      backendContent: "side question",
      conversationId: "side-1",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "side question",
      conversation_id: "side-1",
    });
  });

  it("clears stale turn plan and todos when starting a new user turn", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      isConnected: true,
      isStreaming: false,
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      plan: {
        threadId: "conv-test",
        turnId: "turn-angry-birds",
        plan: [
          { step: "用单文件 HTML 实现愤怒的小鸟游戏", status: "in_progress" },
          { step: "验证游戏文件写入正确", status: "pending" },
        ],
      },
      todos: [
        {
          id: "todo-angry-birds",
          content: "正在编写愤怒的小鸟 HTML 游戏",
          activeForm: "正在编写愤怒的小鸟 HTML 游戏",
          status: "in_progress",
        },
      ],
      agentProgress: [{
        type: "progress",
        id: "plan:angry-birds-plan",
        stage: "planning",
        status: "running",
        message: "执行上一轮计划",
        timestamp: 1,
        conversationId: "conv-test",
      }],
      conversationAgentStates: {
        "conv-test": {
          plan: null,
          todos: [],
          subagents: [],
          agentProgress: [],
        },
      },
    });

    const ok = sendChatMessage({
      displayContent: "换个游戏吧 我要完cf",
      backendContent: "换个游戏吧 我要完cf",
    });

    const state = useAppStore.getState();
    expect(ok).toBe(true);
    expect(state.plan).toBeNull();
    expect(state.todos).toEqual([]);
    expect(state.agentProgress).toEqual([]);
    expect(state.conversationAgentStates["conv-test"]).toMatchObject({
      plan: null,
      todos: [],
      agentProgress: [],
    });
    expect(state.messages.at(-2)).toMatchObject({
      role: "user",
      content: "换个游戏吧 我要完cf",
    });
  });

  it("allows sending to an idle side conversation while the active conversation has a local pending question", () => {
    useAppStore.setState({
      conversationId: "conv-main",
      pendingAskUser: {
        requestId: "ask-main",
        conversationId: "conv-main",
        question: "Continue main?",
      },
      sideChats: {
        "side-1": {
          id: "side-1",
          messages: [],
          draft: "",
          isStreaming: false,
          inheritedContext: "",
        },
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "side question while main waits",
      backendContent: "side question while main waits",
      conversationId: "side-1",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "side question while main waits",
      conversation_id: "side-1",
    });
  });

  it("does not block the current conversation with a local pending prompt from another conversation", () => {
    useAppStore.setState({
      conversationId: "conv-other",
      pendingAskUser: {
        requestId: "ask-main",
        conversationId: "conv-main",
        question: "Continue main?",
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new active prompt",
      backendContent: "new active prompt",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "new active prompt",
      conversation_id: "conv-other",
    });
  });

  it("blocks explicit sends to the conversation that owns a local pending prompt", () => {
    useAppStore.setState({
      conversationId: "conv-other",
      pendingAskUser: {
        requestId: "ask-main",
        conversationId: "conv-main",
        question: "Continue main?",
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new main prompt",
      backendContent: "new main prompt",
      conversationId: "conv-main",
      skipLocalAppend: true,
    });

    expect(ok).toBe(false);
    expect(sent).toHaveLength(0);
  });

  it("blocks sending when the target conversation has a restored pending user action", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      runtimeSession: {
        active_conversation_id: "conv-active",
        pending_approval_count: 1,
        pending_approvals: [{
          request_id: "ask-active",
          type: "ask_user",
          conversation_id: "conv-active",
        }],
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new prompt",
      backendContent: "new prompt",
    });

    expect(ok).toBe(false);
    expect(sent).toHaveLength(0);
  });

  it("allows sending when only another conversation has a restored pending user action", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      runtimeSession: {
        active_conversation_id: "conv-active",
        pending_approval_count: 1,
        pending_approvals: [{
          request_id: "ask-other",
          type: "ask_user",
          conversation_id: "conv-other",
        }],
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "new prompt",
      backendContent: "new prompt",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "new prompt",
      conversation_id: "conv-active",
    });
  });

  it("includes the active conversation id on normal sends", () => {
    useAppStore.setState({
      conversationId: "conv-active-send",
      conversations: [{ id: "conv-active-send", title: "Active", updatedAt: "2026-06-06T00:00:00.000Z" }],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "continue",
      backendContent: "continue",
      skipLocalAppend: true,
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "continue",
      conversation_id: "conv-active-send",
    });
  });

  it("creates a streaming placeholder for an explicit background conversation", () => {
    useAppStore.setState({
      conversationId: "conv-a",
      messages: [{
        id: "assistant-a",
        role: "assistant",
        content: "active thread",
        artifacts: [],
        timestamp: 1,
      }],
      conversationMessages: {
        "conv-b": [],
      },
      conversationStreaming: {
        "conv-a": false,
        "conv-b": false,
      },
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "background question",
      backendContent: "background question",
      conversationId: "conv-b",
    });

    const state = useAppStore.getState();
    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "background question",
      conversation_id: "conv-b",
    });
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].content).toBe("active thread");
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-b"]).toBe(true);
    expect(state.conversationMessages["conv-b"]).toEqual([
      expect.objectContaining({ role: "user", content: "background question" }),
      expect.objectContaining({ role: "assistant", content: "", isStreaming: true }),
    ]);
  });

  it("recovers stale streaming state before sending instead of showing a running error", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        {
          id: "assistant-stale",
          role: "assistant",
          content: "Previous run failed.",
          artifacts: [],
          timestamp: 1,
          isStreaming: false,
        },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "continue",
      backendContent: "continue",
    });

    expect(ok).toBe(true);
    expect(sent).toHaveLength(1);
    expect(useAppStore.getState().messages.some((message) => message.content === "continue")).toBe(true);
  });

  it("allows a short repeated reply after the previous turn has finished", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-05T00:00:00.000Z"));
    useAppStore.setState({
      conversationId: "conv-repeat",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
    });

    expect(sendChatMessage({
      displayContent: "继续",
      backendContent: "继续",
    })).toBe(true);
    useAppStore.getState().finishStreaming("conv-repeat");

    vi.advanceTimersByTime(300);

    expect(sendChatMessage({
      displayContent: "继续",
      backendContent: "继续",
    })).toBe(true);
    expect(sent.filter((command) => (command as { content?: string }).content === "继续")).toHaveLength(2);
  });

  it("sends the current permission and agent modes", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      conversations: [{ id: "conv-test", title: "Project", updatedAt: "2026-06-06T00:00:00.000Z", workspaceRoot: "C:\\Desktop\\PDFTranslate" }],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
      permissionMode: "bypass",
      agentMode: "review",
      workingDirectory: "C:\\Desktop\\PDFTranslate",
    });

    const ok = sendChatMessage({
      displayContent: "write the README",
      backendContent: "write the README",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "write the README",
      permission_mode: "bypass",
      agent_mode: "review",
      workspace_root: "C:\\Desktop\\PDFTranslate",
    });
  });

  it("sends the active editor file as primary workspace grounding", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      conversations: [{ id: "conv-test", title: "Project", updatedAt: "2026-06-06T00:00:00.000Z", workspaceRoot: "C:\\Desktop\\MiniCode" }],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
      workingDirectory: "C:\\Desktop\\MiniCode",
      activeTabPath: "index.html",
    });

    const ok = sendChatMessage({
      displayContent: "看看我的文件",
      backendContent: "看看我的文件",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "看看我的文件",
      workspace_root: "C:\\Desktop\\MiniCode",
      primary_file: "index.html",
      active_tab_path: "index.html",
    });
  });

  it("does not bind the first global message to a stale working directory", () => {
    useAppStore.setState({
      conversationId: null,
      conversations: [],
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
      permissionMode: "bypass",
      workingDirectory: "C:\\Desktop\\MiniCode",
      activeTabPath: "README.md",
    });

    const ok = sendChatMessage({
      displayContent: "hello",
      backendContent: "hello",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "hello",
      permission_mode: "bypass",
    });
    expect(sent[0]).not.toHaveProperty("workspace_root");
    expect(sent[0]).not.toHaveProperty("primary_file");
  });

  it("marks the local streaming turn failed when websocket send throws", () => {
    wsMock.throwOnSend = true;
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [],
      conversationMessages: {},
      conversationStreaming: {},
      isStreaming: false,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "hello",
      backendContent: "hello",
    });

    const state = useAppStore.getState();
    expect(ok).toBe(false);
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-test"]).toBe(false);
    expect(state.messages.some((message) => message.role === "system" && /socket closed/i.test(message.content))).toBe(true);
  });

  it("preserves pasted-text semantics when an attachment-only message is recalled", async () => {
    useAppStore.getState().sendMessage("", {
      assistant: false,
      attachmentRefs: [{
        id: "artifact-paste-recall",
        name: "pasted-9.txt",
        kind: "document",
        mediaType: "text/plain",
        artifactId: "artifact-paste-recall",
        sizeBytes: 30_000,
        inputSource: "pasted_text",
        sourceCharCount: 30_000,
      }],
    });

    const userMessage = useAppStore.getState().messages[0];
    await useAppStore.getState().recallMessage(userMessage.id);

    expect(useAppStore.getState().draft).toBe("");
    expect(useAppStore.getState().attachments[0]).toMatchObject({
      inputSource: "pasted_text",
      sourceCharCount: 30_000,
      attachment: {
        input_source: "pasted_text",
        source_char_count: 30_000,
      },
    });
  });

  it("treats a rejected websocket send as a failed local turn", () => {
    wsMock.acceptSend = false;

    const ok = sendChatMessage({
      displayContent: "rejected transport",
      backendContent: "rejected transport",
    });

    const state = useAppStore.getState();
    expect(ok).toBe(false);
    expect(state.isStreaming).toBe(false);
    expect(state.messages.some((message) => message.role === "system" && /后端连接尚未就绪/.test(message.content))).toBe(true);
  });

  it("hands explicit messages to the websocket queue while reconnecting", () => {
    useAppStore.setState({ isConnected: false });

    const ok = sendChatMessage({
      displayContent: "continue when connected",
      backendContent: "continue when connected",
    });

    expect(ok).toBe(true);
    expect(sent[0]).toMatchObject({
      type: "user_message",
      content: "continue when connected",
    });
  });

  it("rolls back a failed queued send without stopping the current turn", () => {
    wsMock.throwOnSend = true;
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "assistant-running",
        role: "assistant",
        content: "Current answer",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    const ok = sendChatMessage({
      displayContent: "queue this",
      backendContent: "queue this",
      allowWhileStreaming: true,
    });

    const state = useAppStore.getState();
    expect(ok).toBe(false);
    expect(state.isStreaming).toBe(true);
    expect(state.conversationStreaming["conv-test"]).toBe(true);
    expect(state.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "assistant-running", isStreaming: true }),
    ]));
    expect(state.messages.some((message) => message.content === "queue this")).toBe(false);
  });

  it("interrupt clears message-level streaming state immediately", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        {
          id: "assistant-running",
          role: "assistant",
          content: "",
          artifacts: [],
          blocks: [{
            type: "progress",
            id: "agent:model:1",
            stage: "planning",
            status: "running",
            message: "Choosing the next step",
            timestamp: 1,
          }],
          timestamp: 1,
          isStreaming: true,
          isThinkingStreaming: true,
        },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().interrupt();

    const state = useAppStore.getState();
    const assistant = state.messages[0];
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-test"]).toBe(false);
    expect(assistant.isStreaming).toBe(false);
    expect(assistant.isThinkingStreaming).toBe(false);
    expect(assistant.blocks?.[0]).toMatchObject({
      status: "partial",
      label: "已中断",
      summary: "用户已中断",
    });
  });

  it("finishStreaming clears thinking-only assistant state for targeted done events", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        {
          id: "assistant-thinking-only",
          role: "assistant",
          content: "Hi!",
          artifacts: [],
          blocks: [
            {
              type: "thinking",
              source: "provider",
              content: "正在整理回复",
            },
            {
              type: "text",
              content: "Hi!",
              source: "model_final",
              visibility: "final",
              phase: "final",
            },
          ],
          timestamp: 1,
          isStreaming: false,
          isThinkingStreaming: true,
        },
      ],
      conversationMessages: {
        "conv-test": [
          {
            id: "assistant-thinking-only",
            role: "assistant",
            content: "Hi!",
            artifacts: [],
            blocks: [
              {
                type: "thinking",
                source: "provider",
                content: "正在整理回复",
              },
              {
                type: "text",
                content: "Hi!",
                source: "model_final",
                visibility: "final",
                phase: "final",
              },
            ],
            timestamp: 1,
            isStreaming: false,
            isThinkingStreaming: true,
          },
        ],
      },
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().finishStreaming("conv-test", undefined, "completed", "assistant-thinking-only");

    const state = useAppStore.getState();
    const assistant = state.messages[0];
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-test"]).toBe(false);
    expect(assistant.isStreaming).toBe(false);
    expect(assistant.isThinkingStreaming).toBe(false);
    expect(assistant.terminalStatus).toBe("completed");
    expect(assistant.blocks).toEqual([
      expect.objectContaining({ type: "text", content: "Hi!" }),
    ]);
  });

  it("finishStreaming retains a provider reasoning summary", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "assistant-summary",
        role: "assistant",
        content: "Done.",
        artifacts: [],
        blocks: [
          {
            type: "thinking",
            source: "provider",
            providerReasoningType: "reasoning_summary_text",
            content: "Checked the implementation.",
          },
          { type: "text", content: "Done.", source: "model_final" },
        ],
        timestamp: 1,
        isStreaming: true,
        isThinkingStreaming: true,
      }],
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().finishStreaming("conv-test", undefined, "completed", "assistant-summary");

    expect(useAppStore.getState().messages[0]?.blocks).toEqual([
      expect.objectContaining({
        type: "thinking",
        content: "Checked the implementation.",
        providerReasoningType: "reasoning_summary_text",
      }),
      expect.objectContaining({ type: "text", content: "Done." }),
    ]);
  });

  it("marks unfinished tools cancelled when the user interrupts a turn", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [{
        id: "assistant-running-tool",
        role: "assistant",
        content: "",
        artifacts: [],
        blocks: [{
          type: "tool_call",
          record: {
            id: "tool-running",
            name: "run_command",
            args: {},
            status: "running",
            startedAt: 1,
          },
        }],
        timestamp: 1,
        isStreaming: true,
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      isConnected: true,
    });

    useAppStore.getState().finishStreaming("conv-test", undefined, "interrupted", "assistant-running-tool");

    const block = useAppStore.getState().messages[0].blocks?.[0];
    expect(block?.type).toBe("tool_call");
    if (block?.type === "tool_call") expect(block.record.status).toBe("cancelled");
  });

  it("retries atomically while streaming without queueing behind the old run", () => {
    useAppStore.setState({
      conversationId: "conv-test",
      messages: [
        { id: "user-retry", role: "user", content: "try again", artifacts: [], timestamp: 1 },
        { id: "assistant-old", role: "assistant", content: "old", artifacts: [], timestamp: 2 },
        { id: "assistant-running", role: "assistant", content: "newer", artifacts: [], timestamp: 3, isStreaming: true },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-test": true },
      isStreaming: true,
      pendingApproval: { requestId: "approval-old", conversationId: "conv-test", toolName: "run_command", args: {} },
    });

    const ok = sendChatMessage({
      displayContent: "try again",
      retryFromMessageId: "user-retry",
    });

    expect(ok).toBe(true);
    expect(sent).toEqual([expect.objectContaining({
      type: "user_message",
      content: "try again",
      conversation_id: "conv-test",
      retry_from_message_id: "user-retry",
    })]);
    expect(sent[0]).not.toHaveProperty("queue_if_busy");
    expect(useAppStore.getState().messages.map((message) => message.content)).toEqual(["try again", ""]);
  });

  it("does not rewind the local transcript when an atomic retry cannot be sent", () => {
    const originalMessages = [
      { id: "user-retry", role: "user" as const, content: "try again", artifacts: [], timestamp: 1 },
      { id: "assistant-old", role: "assistant" as const, content: "old", artifacts: [], timestamp: 2 },
    ];
    useAppStore.setState({ messages: originalMessages, isStreaming: false });
    wsMock.acceptSend = false;

    expect(sendChatMessage({
      displayContent: "try again",
      retryFromMessageId: "user-retry",
    })).toBe(false);

    expect(useAppStore.getState().messages.slice(0, originalMessages.length)).toEqual(originalMessages);
    expect(useAppStore.getState().messages.at(-1)).toEqual(expect.objectContaining({
      role: "system",
      content: "错误：后端连接尚未就绪，请稍后重试。",
    }));
  });

});
