import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleSessionEvent } from "./sessionEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { getThinkingFromMessage, getToolCallsFromMessage } from "../lib/content-blocks";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
}));

const makeBuffer = (): StreamBuffer & { destroy: ReturnType<typeof vi.fn> } => ({
  push: vi.fn(),
  flush: vi.fn(),
  destroy: vi.fn(),
});

describe("handleSessionEvent", () => {
  beforeEach(() => {
    vi.mocked(sendClientCommand).mockClear();
    vi.mocked(pushToast).mockClear();
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [],
      conversationInventoryInstanceId: null,
      conversationInventoryRevision: 0,
      activeGoal: null,
      messages: [{
        id: "streaming",
        role: "assistant",
        content: "",
        blocks: [{ type: "thinking", content: "partial" }],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
        isThinkingStreaming: true,
      }],
      conversationMessages: {
        "conv-cached": [{
          id: "cached-streaming",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
          isThinkingStreaming: true,
        }],
      },
      conversationStreaming: {
        "conv-active": true,
        "conv-cached": true,
      },
      conversationRecallTruncations: {},
      isStreaming: true,
      currentModel: "",
      currentProvider: "openai",
      currentProviderId: "openai_official",
      currentProviderBaseUrl: "https://api.openai.com/v1",
      currentWireApi: "responses",
      permissionMode: "confirm",
      runtimeSession: null,
      workingDirectory: null,
      conversationHydration: {},
      permissionRulesByConversation: {},
      checkpointsByConversation: {},
      runCheckpointsByConversation: {},
      checkpointResumeByConversation: {},
      guidelineReloadsByConversation: {},
      providerOAuthFlowsByConversation: {},
    });
  });

  it("stores provider OAuth authorization state by owner and only toasts the active live owner", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    const activeEvent = {
      type: "llm.provider.oauth.auth",
      conversation_id: "conv-active",
      provider: "github-copilot",
      url: "https://github.com/login/oauth/authorize?state=expected",
      instructions: "Complete authorization in the browser.",
      timestamp: "2026-08-15T08:00:00.000Z",
      seq: 10,
    };

    expect(handleSessionEvent(activeEvent as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().providerOAuthFlowsByConversation["conv-active"]?.["github-copilot"]).toMatchObject({
      conversationId: "conv-active",
      provider: "github-copilot",
      phase: "auth_url",
      url: activeEvent.url,
      instructions: activeEvent.instructions,
      updatedAt: Date.parse("2026-08-15T08:00:00.000Z"),
      eventSeq: 10,
    });
    expect(pushToast).toHaveBeenCalledTimes(1);

    vi.mocked(pushToast).mockClear();
    expect(handleSessionEvent({
      ...activeEvent,
      conversation_id: "conv-inactive",
      seq: 11,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().providerOAuthFlowsByConversation["conv-inactive"]?.["github-copilot"]).toBeTruthy();
    expect(pushToast).not.toHaveBeenCalled();

    expect(handleSessionEvent({
      ...activeEvent,
      seq: 12,
      replayed: true,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().providerOAuthFlowsByConversation["conv-active"]?.["github-copilot"]?.eventSeq).toBe(12);
    expect(pushToast).not.toHaveBeenCalled();
  });

  it("projects device-code, info, progress, expiry and links without losing useful OAuth state", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    const timestamp = "2026-08-15T08:10:00.000Z";

    expect(handleSessionEvent({
      type: "llm.provider.oauth.device_code",
      conversation_id: "conv-active",
      provider: "openai",
      userCode: "ABCD-EFGH",
      verificationUri: "https://auth.openai.com/device",
      intervalSeconds: 5,
      expiresInSeconds: 900,
      timestamp,
      seq: 20,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().providerOAuthFlowsByConversation["conv-active"]?.["openai"]).toMatchObject({
      phase: "device_code",
      userCode: "ABCD-EFGH",
      verificationUri: "https://auth.openai.com/device",
      intervalSeconds: 5,
      expiresInSeconds: 900,
      expiresAt: Date.parse(timestamp) + 900_000,
      eventSeq: 20,
    });

    expect(handleSessionEvent({
      type: "llm.provider.oauth.info",
      conversation_id: "conv-active",
      provider: "openai",
      message: "Approve the device request.",
      links: [{ url: "https://help.openai.com/oauth", label: "Help" }],
      timestamp: "2026-08-15T08:10:01.000Z",
      seq: 21,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(handleSessionEvent({
      type: "llm.provider.oauth.progress",
      conversation_id: "conv-active",
      provider: "openai",
      message: "Waiting for approval.",
      timestamp: "2026-08-15T08:10:02.000Z",
      seq: 22,
    } as unknown as ServerEvent, buffers)).toBe(true);

    expect(useAppStore.getState().providerOAuthFlowsByConversation["conv-active"]?.["openai"]).toMatchObject({
      phase: "progress",
      userCode: "ABCD-EFGH",
      verificationUri: "https://auth.openai.com/device",
      expiresAt: Date.parse(timestamp) + 900_000,
      message: "Waiting for approval.",
      links: [{ url: "https://help.openai.com/oauth", label: "Help" }],
      eventSeq: 22,
    });
  });

  it("rejects older sequenced and unsequenced OAuth projections and clears them with the owner", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    expect(handleSessionEvent({
      type: "llm.provider.oauth.progress",
      conversation_id: "conv-active",
      provider: "anthropic",
      message: "Newest progress",
      timestamp: "2026-08-15T09:00:10.000Z",
      seq: 30,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(handleSessionEvent({
      type: "llm.provider.oauth.info",
      conversation_id: "conv-active",
      provider: "anthropic",
      message: "Older sequenced info",
      timestamp: "2026-08-15T09:00:11.000Z",
      seq: 29,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(handleSessionEvent({
      type: "llm.provider.oauth.info",
      conversation_id: "conv-active",
      provider: "anthropic",
      message: "Older unsequenced info",
      timestamp: "2026-08-15T09:00:00.000Z",
    } as unknown as ServerEvent, buffers)).toBe(true);

    expect(useAppStore.getState().providerOAuthFlowsByConversation["conv-active"]?.anthropic).toMatchObject({
      phase: "progress",
      message: "Newest progress",
      eventSeq: 30,
    });

    useAppStore.getState().clearConversationControlPlaneState("conv-active");
    expect(useAppStore.getState().providerOAuthFlowsByConversation["conv-active"]).toBeUndefined();
  });

  it("requests durable hydration when replay reports missed events", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };

    expect(handleSessionEvent({
      type: "session.restored",
      active_conversation_id: "conv-active",
      missed_events: true,
      session: { active_conversation_id: "conv-active" },
    } as unknown as ServerEvent, buffers)).toBe(true);

    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "conversation.switch",
      conversation_id: "conv-active",
    });
  });

  it("updates the effort selector only from the effective provider value", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    useAppStore.setState({ effortLevel: "high" });

    expect(handleSessionEvent({
      type: "llm.model.updated",
      provider: "custom",
      current_model: "deepseek-v4-flash",
      reasoning_effort: "low",
      configured_reasoning_effort: "low",
      effective_reasoning_effort: "",
      reasoning_effort_supported: false,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().effortLevel).toBe("high");

    expect(handleSessionEvent({
      type: "llm.model.updated",
      provider: "custom",
      current_model: "provider-model",
      configured_reasoning_effort: "focused",
      effective_reasoning_effort: "focused",
      reasoning_effort_supported: true,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().effortLevel).toBe("focused");
  });

  it("records the backend hydration phase from conversation switched events", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-hydrating",
      is_hydrating: true,
      conversation: {
        id: "conv-hydrating",
        title: "Hydrating",
        updated_at: "2026-08-15T00:00:00Z",
        workspace_root: "C:/repo",
      },
    } as unknown as ServerEvent, buffers)).toBe(true);

    expect(useAppStore.getState().conversationHydration["conv-hydrating"]).toMatchObject({
      isHydrating: true,
      updatedAt: expect.any(Number),
    });
    expect(sendClientCommand).toHaveBeenCalledOnce();
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "commands.list" });
  });

  it("does not refresh the command catalog for a replayed conversation switch", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-replayed",
      is_hydrating: false,
      conversation: {
        id: "conv-replayed",
        title: "Replayed",
        updated_at: "2026-08-15T00:00:00Z",
        workspace_root: "C:/repo",
        messages: [],
      },
      replayed: true,
    } as unknown as ServerEvent, buffers)).toBe(true);

    expect(useAppStore.getState().conversationId).toBe("conv-replayed");
    expect(sendClientCommand).not.toHaveBeenCalled();
  });

  it("rehydrates Provider Inspector traces from the durable transcript", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    useAppStore.setState({
      inspectorEntries: [{
        targetKind: "provider",
        targetId: "stale-trace",
        payload: { kind: "provider_trace", finish_reason: "unknown" },
        timestamp: 1,
      }],
      inspectorFocus: { kind: "provider", id: "stale-trace" },
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-provider-trace",
      conversation: {
        id: "conv-provider-trace",
        title: "Provider trace restore",
        updated_at: "2026-08-16T11:00:00Z",
        messages: [{
          id: "assistant-provider-trace",
          role: "assistant",
          content: "done",
          completed_at: "2026-08-16T11:00:01Z",
          usage: {
            input_tokens: 32,
            output_tokens: 12,
            cache_read_input_tokens: 7,
            reasoning_output_tokens: 3,
          },
          blocks: [{
            type: "text",
            itemId: "provider-message",
            content: "done",
            status: "completed",
            finishReason: "completed",
            providerRaw: {
              provider: "openai_responses",
              model: "gpt-5.5-audit",
              event_type: "response.completed",
              trace_id: "run-restore:iter:1:provider:1",
              iteration_id: "iter:1",
              call_index: 1,
              diagnostics_deferred: true,
              diagnostics_ref: "provider:run-restore:iter:1:provider:1",
              diagnostics_bytes: 4096,
              provider_timeline: [{ event: "response.completed", finish_reason: "completed" }],
              safety: { redacted_prompt: true },
            },
          }],
        }],
      },
    } as unknown as ServerEvent, buffers)).toBe(true);

    expect(useAppStore.getState().inspectorFocus).toBeNull();
    expect(useAppStore.getState().inspectorEntries).toEqual([expect.objectContaining({
      targetKind: "provider",
      targetId: "run-restore:iter:1:provider:1",
      payload: expect.objectContaining({
        kind: "provider_trace",
        provider: "openai_responses",
        model: "gpt-5.5-audit",
        finish_reason: "completed",
        event_type: "response.completed",
        diagnostics_deferred: true,
        diagnostics_ref: "provider:run-restore:iter:1:provider:1",
        diagnostics_bytes: 4096,
        conversationId: "conv-provider-trace",
        messageId: "assistant-provider-trace",
        restored_from: "transcript",
        usage: expect.objectContaining({
          input_tokens: 32,
          output_tokens: 12,
          cache_read_input_tokens: 7,
          cache_creation_input_tokens: 0,
          reasoning_output_tokens: 3,
        }),
      }),
    })]);
  });

  it("projects persisted conversation summaries into session metadata", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-active",
      conversations: [{
        id: "conv-active",
        title: "MiniCode release audit",
        updated_at: "2026-08-15T10:00:00Z",
        summary: "User: audit all agents | Assistant: event contracts aligned",
        compaction_state: "compacted",
      }],
      active_conversation: {
        id: "conv-active",
        title: "MiniCode release audit",
        updated_at: "2026-08-15T10:00:00Z",
        summary: "User: audit all agents | Assistant: event contracts aligned",
        compaction_state: "compacted",
        messages: [],
      },
    } as unknown as ServerEvent, buffers)).toBe(true);

    expect(useAppStore.getState().conversations[0]).toMatchObject({
      id: "conv-active",
      summary: "User: audit all agents | Assistant: event contracts aligned",
      compactionState: "compacted",
    });
  });

  it("rehydrates durable queued follow-ups from the runtime snapshot", () => {
    handleSessionEvent({
      type: "session.synced",
      session: {
        active_conversation_id: "conv-active",
        queued_user_messages: [{
          conversation_id: "conv-active",
          message_id: "assistant-replayed",
          user_message_id: "user-replayed",
          content: "继续处理剩余问题",
          position: 1,
        }],
      },
    } as unknown as ServerEvent, { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() });

    expect(useAppStore.getState().messages).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: "user-replayed",
        queueState: "queued",
        queuePosition: 1,
      }),
      expect.objectContaining({
        id: "assistant-replayed",
        queueState: "queued",
        queuePosition: 1,
      }),
    ]));
  });

  it("moves an optimistic queued turn into streaming only when the backend dequeues it", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [
        { id: "user-next", role: "user", content: "next", artifacts: [], timestamp: 2, queueState: "queued", queueMessageId: "assistant-next" },
        { id: "assistant-next", role: "assistant", content: "", artifacts: [], timestamp: 2, isStreaming: false, queueState: "queued", queueMessageId: "assistant-next" },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-active": false },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "user_message.queue.updated",
      status: "dequeued",
      conversation_id: "conv-active",
      user_message_id: "user-next",
      message_id: "assistant-next",
    } as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.isStreaming).toBe(true);
    expect(state.messages[0]).toMatchObject({ id: "user-next", queueState: undefined });
    expect(state.messages[1]).toMatchObject({ id: "assistant-next", isStreaming: true, queueState: undefined });
  });

  it("folds a steered queued message into the existing streaming turn", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [
        { id: "user-current", role: "user", content: "first", artifacts: [], timestamp: 1 },
        { id: "assistant-current", role: "assistant", content: "partial", artifacts: [], timestamp: 1, isStreaming: true },
        { id: "user-steer", role: "user", content: "change direction", artifacts: [], timestamp: 2, queueState: "queued", queueMessageId: "assistant-steer" },
        { id: "assistant-steer", role: "assistant", content: "", artifacts: [], timestamp: 2, isStreaming: false, queueState: "queued", queueMessageId: "assistant-steer" },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-active": true },
      isStreaming: true,
      agentProgress: [{
        type: "progress",
        id: "existing-progress",
        stage: "status",
        status: "running",
        message: "working",
        timestamp: 1,
      }],
    });

    handleSessionEvent({
      type: "user_message.queue.updated",
      status: "dequeued",
      reason: "steered_current_turn",
      turn_mode: "steer",
      conversation_id: "conv-active",
      user_message_id: "user-steer",
      message_id: "assistant-steer",
      target_message_id: "assistant-current",
    } as ServerEvent, { textStreamBuffer, thinkingStreamBuffer });

    const state = useAppStore.getState();
    expect(state.isStreaming).toBe(true);
    expect(state.messages.find((message) => message.id === "assistant-current")).toMatchObject({ isStreaming: true });
    expect(state.messages.find((message) => message.id === "assistant-steer")).toBeUndefined();
    expect(state.messages.find((message) => message.id === "user-steer")).toMatchObject({
      queueState: undefined,
      steeredIntoMessageId: "assistant-current",
    });
    expect(state.agentProgress).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "existing-progress" }),
    ]));
  });

  it("restores an unconsumed steer from the canonical runtime snapshot", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [
        { id: "user-current", role: "user", content: "first", artifacts: [], timestamp: 1 },
        { id: "assistant-current", role: "assistant", content: "partial", artifacts: [], timestamp: 2, isStreaming: true },
      ],
      conversationMessages: {
        "conv-active": [
          { id: "user-current", role: "user", content: "first", artifacts: [], timestamp: 1 },
          { id: "assistant-current", role: "assistant", content: "partial", artifacts: [], timestamp: 2, isStreaming: true },
        ],
      },
      conversationStreaming: { "conv-active": true },
      isStreaming: true,
    });

    handleSessionEvent({
      type: "session.synced",
      active_conversation_id: "conv-active",
      session: {
        session_id: "session-active",
        active_conversation_id: "conv-active",
        active_stream_conversation_ids: ["conv-active"],
        pending_turn_inputs: [{
          conversation_id: "conv-active",
          mode: "steer",
          message_id: "assistant-steer",
          user_message_id: "user-steer",
          target_message_id: "assistant-current",
          content: "change direction",
          attachments: [{
            file_name: "notes.txt",
            artifact_id: "artifact-notes",
            media_type: "text/plain",
          }],
          queued_at_ms: 3,
        }],
      },
    } as ServerEvent, { textStreamBuffer, thinkingStreamBuffer });

    const state = useAppStore.getState();
    expect(state.messages.map((message) => message.id)).toEqual([
      "user-current",
      "user-steer",
      "assistant-current",
    ]);
    expect(state.messages[1]).toMatchObject({
      content: "change direction",
      steeredIntoMessageId: "assistant-current",
      attachmentRefs: [expect.objectContaining({
        name: "notes.txt",
        artifactId: "artifact-notes",
      })],
    });
    expect(state.messages[1]?.queueState).toBeUndefined();
    expect(state.isStreaming).toBe(true);
    expect(state.messages[2]).toMatchObject({ id: "assistant-current", isStreaming: true });
  });

  it("removes a cancelled queued prompt and its empty assistant placeholder", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [
        { id: "user-next", role: "user", content: "next", artifacts: [], timestamp: 2, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-next" },
        { id: "assistant-next", role: "assistant", content: "", artifacts: [], timestamp: 2, isStreaming: false, queueState: "queued", queuePosition: 1, queueMessageId: "assistant-next" },
        { id: "user-later", role: "user", content: "later", artifacts: [], timestamp: 3, queueState: "queued", queuePosition: 2, queueMessageId: "assistant-later" },
        { id: "assistant-later", role: "assistant", content: "", artifacts: [], timestamp: 3, isStreaming: false, queueState: "queued", queuePosition: 2, queueMessageId: "assistant-later" },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-active": true },
      isStreaming: true,
    });

    handleSessionEvent({
      type: "user_message.queue.updated",
      status: "cancelled",
      conversation_id: "conv-active",
      user_message_id: "user-next",
      message_id: "assistant-next",
    } as ServerEvent, { textStreamBuffer, thinkingStreamBuffer });

    expect(useAppStore.getState().messages).toEqual([
      expect.objectContaining({ id: "user-later", queueState: "queued", queuePosition: 1 }),
      expect.objectContaining({ id: "assistant-later", queueState: "queued", queuePosition: 1 }),
    ]);
  });

  it("session restore clears only the active stale streaming state and updates model metadata", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState((state) => ({
      conversationMessages: {
        ...state.conversationMessages,
        "conv-active": state.messages,
      },
    }));

    expect(handleSessionEvent({
      type: "session.restored",
      model: "gpt-5",
      provider: "openai",
      provider_id: "openai_official",
      base_url: "https://api.openai.com/v1",
      wire_api: "responses",
      working_directory: "C:/work/project",
      available_models: ["gpt-5"],
      active_conversation_id: "conv-active",
      active_conversation: {
        id: "conv-active",
        title: "Active",
        updated_at: "2026-01-01T00:00:00.000Z",
        workspace_root: "C:/work/project",
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(textStreamBuffer.destroy).toHaveBeenCalledOnce();
    expect(thinkingStreamBuffer.destroy).toHaveBeenCalledOnce();
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming).toEqual({
      "conv-active": false,
      "conv-cached": true,
    });
    expect(state.messages[0].isStreaming).toBe(false);
    expect(state.messages[0].isThinkingStreaming).toBe(false);
    expect(state.conversationMessages["conv-active"][0].isStreaming).toBe(false);
    expect(state.conversationMessages["conv-active"][0].isThinkingStreaming).toBe(false);
    expect(state.conversationMessages["conv-cached"][0].isStreaming).toBe(true);
    expect(state.conversationMessages["conv-cached"][0].isThinkingStreaming).toBe(true);
    expect(state.currentModel).toBe("gpt-5");
    expect(state.currentProviderId).toBe("openai_official");
    expect(state.currentProviderBaseUrl).toBe("https://api.openai.com/v1");
    expect(state.currentWireApi).toBe("responses");
    expect(state.workingDirectory).toBe("C:/work/project");
  });

  // Regression: clearing the two message-level streaming flags is not enough to
  // seal a turn. A tool_call record left at "running" keeps ExecCell spinning
  // with a live Stop button on a turn the transcript already renders as over,
  // so the restore path has to go through the canonical `finishStreaming`.
  it("session restore terminalizes blocks, tool records and terminal status, not just message flags", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      messages: [{
        id: "assistant-live",
        role: "assistant",
        content: "",
        blocks: [
          { type: "tool_call", record: { id: "tc-live", name: "run_command", status: "running", args: {} } },
          { type: "progress", id: "prog-live", label: "运行中", status: "running", timestamp: 1 },
          { type: "text", itemId: "item-live", content: "half an answer", status: "in_progress", isStreaming: true },
        ],
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-active": true },
      isStreaming: true,
    } as never);

    expect(handleSessionEvent({
      type: "session.restored",
      active_conversation_id: "conv-active",
      session: {
        active_conversation_id: "conv-active",
        active_task_id: null,
        active_stream_conversation_ids: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const message = useAppStore.getState().messages[0];
    expect(message).toMatchObject({
      isStreaming: false,
      isThinkingStreaming: false,
      terminalStatus: "partial",
    });
    expect(message.completedAt).toBeTypeOf("number");
    expect(getToolCallsFromMessage(message)[0]).toMatchObject({ id: "tc-live", status: "partial" });
    const blocks = message.blocks ?? [];
    expect(blocks.find((block) => block.type === "progress")).toMatchObject({ status: "partial" });
    expect(blocks.find((block) => block.type === "text")).toMatchObject({
      isStreaming: false,
      status: "partial",
    });
  });

  it("session restore preserves streaming while the backend still owns an active task", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState((state) => ({
      conversations: [{ id: "conv-active", title: "Active", updatedAt: "2026-01-01T00:00:00.000Z" }],
      conversationMessages: {
        ...state.conversationMessages,
        "conv-active": state.messages,
      },
    }));

    expect(handleSessionEvent({
      type: "session.restored",
      active_conversation_id: "conv-active",
      session: {
        active_conversation_id: "conv-active",
        active_task_id: "task-running",
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(textStreamBuffer.destroy).toHaveBeenCalledOnce();
    expect(thinkingStreamBuffer.destroy).toHaveBeenCalledOnce();
    expect(state.isStreaming).toBe(true);
    expect(state.conversationStreaming["conv-active"]).toBe(true);
    expect(state.messages[0].isStreaming).toBe(true);
  });

  it("filters stale fallback models from unknown custom gateway session events", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "session.restored",
      model: "mimo-v2.5-pro",
      provider: "custom",
      provider_id: "custom_openai",
      base_url: "https://api.bbe.to/v1",
      wire_api: "chat",
      available_models: ["gpt-5.5", "gpt-5.4", "mimo-v2.5-pro"],
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.currentModel).toBe("mimo-v2.5-pro");
    expect(state.availableModels).toEqual(["mimo-v2.5-pro"]);
  });

  it("session restore stores runtime snapshot pending prompts and permission mode", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "session.restored",
      current_model: "gpt-5",
      session: {
        session_id: "session-restore",
        selected_model: "gpt-5",
        permission_mode: "bypass",
        permission_profile: "bypass",
        workspace_scope: "computer",
        sandbox_status: { os: "disabled", network: "enabled" },
        pending_approval_count: 1,
        pending_approvals: [{
          request_id: "ask-restore",
          type: "ask_user",
          subtype: "elicitation",
        }],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.runtimeSession).toMatchObject({
      session_id: "session-restore",
      permission_profile: "bypass",
      workspace_scope: "computer",
      sandbox_status: { os: "disabled", network: "enabled" },
      pending_approval_count: 1,
      pending_approvals: [expect.objectContaining({ request_id: "ask-restore" })],
    });
    expect(state.permissionMode).toBe("bypass");
  });

  it("session restore hydrates restored conversation messages and workspace", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "session.restored",
      model: "gpt-5",
      provider: "openai",
      active_conversation_id: "conv-restored",
      conversation: {
        id: "conv-restored",
        title: "Restored",
        updated_at: "2026-01-02T00:00:00.000Z",
        workspace_root: "C:/repo-restored",
      },
      workspace: { root_path: "C:/repo-restored" },
      messages: [{
        id: "assistant-restored",
        role: "assistant",
        content: "final restored",
        blocks: [
          { type: "tool_call", record: { id: "tc-restored", name: "read_file", status: "success", args: {} } },
          { type: "text", content: "final restored" },
        ],
      }],
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-restored");
    expect(state.workingDirectory).toBe("C:/repo-restored");
    expect(state.conversations[0]).toMatchObject({ id: "conv-restored", title: "Restored" });
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].content).toBe("final restored");
    expect(getToolCallsFromMessage(state.messages[0])).toEqual([
      expect.objectContaining({ id: "tc-restored", status: "success" }),
    ]);
  });

  it("session restore defers conversation hydration when a canonical switch event follows", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      workingDirectory: "C:/repo-active",
      conversations: [{
        id: "conv-active",
        title: "Active",
        updatedAt: "2026-01-01T00:00:00.000Z",
        workspaceRoot: "C:/repo-active",
      }],
    });

    expect(handleSessionEvent({
      type: "session.restored",
      current_model: "gpt-5",
      workspace: { root_path: "C:/repo-restored" },
      active_conversation_id: "conv-restored",
      conversation_switched_follows: true,
      active_conversation: {
        id: "conv-restored",
        title: "Restored",
        updated_at: "2026-01-02T00:00:00.000Z",
        workspace_root: "C:/repo-restored",
      },
      session: {
        session_id: "session-restore",
        active_conversation_id: "conv-restored",
      },
      messages: [{
        id: "assistant-restored",
        role: "assistant",
        content: "final restored",
      }],
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.currentModel).toBe("gpt-5");
    expect(state.runtimeSession).toMatchObject({
      session_id: "session-restore",
      active_conversation_id: "conv-restored",
    });
    expect(state.conversationId).toBe("conv-active");
    expect(state.workingDirectory).toBe("C:/repo-active");
    expect(state.messages.map((message) => message.id)).toEqual(["streaming"]);
    expect(state.conversationMessages["conv-restored"]).toBeUndefined();
  });

  it("completed reconnect snapshot clears stale streaming before canonical hydration", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-restored",
      conversations: [{
        id: "conv-restored",
        title: "Restored",
        updatedAt: "2026-01-02T00:00:00.000Z",
      }],
      messages: [{
        id: "assistant-restored",
        role: "assistant",
        content: "partial",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
        isThinkingStreaming: true,
      }],
      conversationMessages: {
        "conv-restored": [{
          id: "assistant-restored",
          role: "assistant",
          content: "partial",
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
          isThinkingStreaming: true,
        }],
        "conv-other-stale": [{
          id: "assistant-other",
          role: "assistant",
          content: "stale",
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        }],
      },
      conversationStreaming: {
        "conv-restored": true,
        "conv-other-stale": true,
      },
      isStreaming: true,
    });

    expect(handleSessionEvent({
      type: "session.restored",
      active_conversation_id: "conv-restored",
      conversation_switched_follows: true,
      session: {
        session_id: "session-reconnect-completed",
        active_conversation_id: "conv-restored",
        active_task_id: null,
        active_stream_conversation_ids: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    let state = useAppStore.getState();
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming).toMatchObject({
      "conv-restored": false,
      "conv-other-stale": false,
    });
    expect(state.messages[0]).toMatchObject({
      id: "assistant-restored",
      isStreaming: false,
      isThinkingStreaming: false,
    });
    expect(state.conversationMessages["conv-other-stale"][0].isStreaming).toBe(false);

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-restored",
      conversation: {
        id: "conv-restored",
        title: "Restored",
        updated_at: "2026-01-02T00:00:01.000Z",
        transcript: [
          {
            id: "user-restored",
            role: "user",
            content: "verify reconnect",
            timestamp: 1,
          },
          {
            id: "assistant-restored",
            role: "assistant",
            content: "completed answer",
            terminal_status: "completed",
            timestamp: 2,
            blocks: [{
              type: "text",
              itemId: "agent-message",
              content: "completed answer",
              source: "model_final",
              status: "completed",
              isStreaming: false,
            }],
          },
        ],
        context_snapshot: {
          ui_agent_state: {
            plan: null,
            todos: [],
            subagents: [],
            agentProgress: [{
              id: "provider:web-search",
              stage: "tool",
              status: "completed",
              message: "Web search completed",
              detail: "1 source",
              timestamp: 2,
            }],
          },
        },
      },
      session: {
        session_id: "session-reconnect-completed",
        active_conversation_id: "conv-restored",
        active_task_id: null,
        active_stream_conversation_ids: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    state = useAppStore.getState();
    expect(state.messages.map((message) => message.id)).toEqual([
      "user-restored",
      "assistant-restored",
    ]);
    expect(state.messages[1]).toMatchObject({
      content: "completed answer",
      terminalStatus: "completed",
    });
    expect(Boolean(state.messages[1].isStreaming)).toBe(false);
    expect(state.isStreaming).toBe(false);
    expect(state.conversationStreaming["conv-restored"]).toBe(false);
    expect(state.agentProgress).toEqual([
      expect.objectContaining({
        id: "provider:web-search",
        status: "completed",
        message: "Web search completed",
        detail: "1 source",
      }),
    ]);
  });

  it("keeps completed Provider context through a normal restore switch and trailing inventory list", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    const conversationId = "conv-restored-provider-context";
    useAppStore.setState({
      conversationId,
      conversations: [{
        id: conversationId,
        title: "Optimistic local metadata",
        updatedAt: "2026-08-16T10:05:00.000Z",
        revision: 99,
      }],
      messages: [{
        id: "assistant-provider-context",
        role: "assistant",
        content: "stale partial",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
        isThinkingStreaming: true,
      }],
      conversationMessages: {
        [conversationId]: [{
          id: "assistant-provider-context",
          role: "assistant",
          content: "stale partial",
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
          isThinkingStreaming: true,
        }],
      },
      conversationStreaming: { [conversationId]: true },
      isStreaming: true,
      plan: null,
      todos: [],
      subagents: [],
      agentProgress: [],
      conversationAgentStates: {
        [conversationId]: {
          plan: null,
          todos: [],
          subagents: [],
          agentProgress: [],
        },
      },
    });

    expect(handleSessionEvent({
      type: "session.restored",
      cursor_reset: false,
      conversation_switched_follows: true,
      active_conversation_id: conversationId,
      session: {
        session_id: "session-provider-context",
        active_conversation_id: conversationId,
        active_task_id: null,
        active_stream_conversation_ids: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const canonicalConversation = {
      id: conversationId,
      title: "Durable completed turn",
      updated_at: "2026-08-16T10:00:00.000Z",
      revision: 25,
      transcript: [
        {
          id: "user-provider-context",
          role: "user",
          content: "verify completed CC context",
          timestamp: 1,
        },
        {
          id: "assistant-provider-context",
          role: "assistant",
          content: "completed answer",
          terminal_status: "completed",
          timestamp: 2,
        },
      ],
      context_snapshot: {
        ui_agent_state: {
          plan: null,
          todos: [],
          subagents: [],
          agentProgress: [{
            type: "progress",
            id: "provider:anthropic_web_audit_1",
            stage: "tool",
            phase: "tool",
            status: "completed",
            message: "Web search completed — 1 source",
            label: "Web search",
            summary: "Web search completed — 1 source",
            visibility: "timeline",
            detail: "Input: 50 characters",
            count: 1,
          }],
        },
      },
    };

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: conversationId,
      conversation: canonicalConversation,
      session: {
        session_id: "session-provider-context",
        active_conversation_id: conversationId,
        active_task_id: null,
        active_stream_conversation_ids: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    expect(useAppStore.getState().agentProgress).toEqual([
      expect.objectContaining({
        id: "provider:anthropic_web_audit_1",
        status: "completed",
        message: "Web search completed — 1 source",
        detail: "Input: 50 characters",
        conversationId,
      }),
    ]);

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: conversationId,
      conversations: [{
        id: conversationId,
        title: "Durable completed turn",
        updated_at: "2026-08-16T10:00:00.000Z",
        revision: 25,
      }],
      session: {
        session_id: "session-provider-context",
        active_conversation_id: conversationId,
        active_task_id: null,
        active_stream_conversation_ids: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.messages.map((message) => message.id)).toEqual([
      "user-provider-context",
      "assistant-provider-context",
    ]);
    expect(state.messages[1]).toMatchObject({
      content: "completed answer",
      terminalStatus: "completed",
    });
    expect(Boolean(state.messages[1].isStreaming)).toBe(false);
    expect(state.isStreaming).toBe(false);
    expect(state.agentProgress).toEqual([
      expect.objectContaining({
        id: "provider:anthropic_web_audit_1",
        status: "completed",
        detail: "Input: 50 characters",
        conversationId,
      }),
    ]);
    expect(state.conversationAgentStates[conversationId].agentProgress).toEqual([
      expect.objectContaining({ id: "provider:anthropic_web_audit_1" }),
    ]);
  });

  it("session restore prefers protected worktree paths over base workspace roots", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "session.restored",
      active_conversation_id: "conv-protected",
      conversation: {
        id: "conv-protected",
        title: "Protected",
        updated_at: "2026-01-02T00:00:00.000Z",
        workspace_root: "C:/repo",
        worktree_path: "C:/repo/.minicode/worktrees/conv-protected",
        git_isolated: true,
      },
      workspace: { root_path: "C:/repo/.minicode/worktrees/conv-protected" },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-protected");
    expect(state.workingDirectory).toBe("C:/repo/.minicode/worktrees/conv-protected");
    expect(state.conversations[0]).toMatchObject({
      id: "conv-protected",
      workspaceRoot: "C:/repo",
      worktreePath: "C:/repo/.minicode/worktrees/conv-protected",
      gitIsolated: true,
    });
  });

  it("session sync restores active conversation snapshot without a separate list event", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "session.synced",
      current_model: "gpt-5-mini",
      provider: "openai",
      working_directory: "C:/repo-sync",
      active_conversation_id: "conv-sync",
      active_conversation: {
        id: "conv-sync",
        title: "Synced",
        updated_at: "2026-01-03T00:00:00.000Z",
        workspace_root: "C:/repo-sync",
        messages: [{
          id: "user-sync",
          role: "user",
          content: "continue",
        }],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-sync");
    expect(state.currentModel).toBe("gpt-5-mini");
    expect(state.workingDirectory).toBe("C:/repo-sync");
    expect(state.messages[0]).toMatchObject({ id: "user-sync", role: "user", content: "continue" });
  });

  it("session sync accepts nested snapshot metadata when top-level fields are absent", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "session.synced",
      provider: "openai",
      session: {
        selected_model: "gpt-5-nested",
        workspace_root: "C:/repo-nested",
        active_conversation_id: "conv-nested",
        active_conversation: {
          id: "conv-nested",
          title: "Nested",
          updated_at: "2026-01-05T00:00:00.000Z",
          workspace_root: "C:/repo-nested",
          messages: [{
            id: "assistant-nested",
            role: "assistant",
            content: "nested restored",
          }],
        },
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-nested");
    expect(state.currentModel).toBe("gpt-5-nested");
    expect(state.workingDirectory).toBe("C:/repo-nested");
    expect(state.conversations[0]).toMatchObject({ id: "conv-nested", title: "Nested" });
    expect(state.messages[0]).toMatchObject({ id: "assistant-nested", content: "nested restored" });
  });

  it("normalizes nullable session fields before updating typed store state", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      workingDirectory: "C:/stale-workspace",
      editorTabs: [{
        path: "src/stale.ts",
        content: "",
        original: "",
        loading: false,
        error: null,
      }],
      activeTabPath: "src/stale.ts",
      activeEditorPath: "src/stale.ts",
    });

    expect(handleSessionEvent({
      type: "session.synced",
      current_model: null,
      model: "gpt-5-fallback",
      provider: null,
      working_directory: null,
      workspace_root: null,
      workspace: { root_path: null },
      active_conversation_id: null,
      session: {
        selected_model: null,
        workspace_root: null,
        active_conversation_id: "conv-nullable",
      },
      active_conversation: {
        id: "conv-nullable",
        title: null,
        updated_at: null,
        workspace_root: null,
        git_branch: null,
        worktree_path: null,
        messages: [{
          id: "nullable-message",
          role: "assistant",
          content: "nullable ok",
        }],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.currentModel).toBe("gpt-5-fallback");
    expect(state.currentProvider).toBe("openai");
    expect(state.workingDirectory).toBe("");
    expect(state.editorTabs).toEqual([]);
    expect(state.activeTabPath).toBeNull();
    expect(state.activeEditorPath).toBeNull();
    expect(state.conversationId).toBe("conv-nullable");
    expect(state.conversations[0]).toMatchObject({
      id: "conv-nullable",
      title: "未命名",
      workspaceRoot: undefined,
      gitBranch: undefined,
      worktreePath: undefined,
    });
    expect(state.messages[0]).toMatchObject({ id: "nullable-message", content: "nullable ok" });
  });

  it("hydrates active conversation from blocks-first transcript without inferring final metadata", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-a",
      conversations: [{
        id: "conv-a",
        title: "A",
        updated_at: "2026-01-01T00:00:00.000Z",
        workspace_root: "C:/repo",
      }],
      active_conversation: {
        id: "conv-a",
        workspace_root: "C:/repo",
        messages: [{
          id: "assistant-1",
          role: "assistant",
          content: "final",
          thinking: "legacy thinking",
          tool_calls: [{ id: "legacy", name: "legacy_tool", status: "success" }],
          blocks: [
            { type: "thinking", content: "block reasoning" },
            {
              type: "tool_call",
              record: {
                id: "tc-block",
                name: "read_file",
                status: "success",
                args: {},
                displayHint: "Reading file",
                inputSummary: "App.tsx",
                iterationId: "iter-1",
                phase: "tool",
                durationMs: 42,
              },
            },
            { type: "text", content: "final" },
          ],
        }],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-a");
    expect(state.workingDirectory).toBe("C:/repo");
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0].blocks).toHaveLength(3);
    expect(state.messages[0].blocks?.[0]).toMatchObject({ type: "thinking", content: "block reasoning" });
    expect(state.messages[0].blocks?.[1]).toMatchObject({
      type: "tool_call",
      record: expect.objectContaining({ id: "tc-block", name: "read_file" }),
    });
    expect(state.messages[0].blocks?.[2]).toMatchObject({
      type: "text",
      content: "final",
    });
    expect(state.messages[0].blocks?.[2]).not.toMatchObject({ visibility: "final" });
    expect("thinking" in state.messages[0]).toBe(false);
    expect("toolCalls" in state.messages[0]).toBe(false);
    expect(getThinkingFromMessage(state.messages[0])).toBe("block reasoning");
    expect(getToolCallsFromMessage(state.messages[0])).toEqual([
      expect.objectContaining({
        id: "tc-block",
        name: "read_file",
        displayHint: "Reading file",
        inputSummary: "App.tsx",
        iterationId: "iter-1",
        phase: "tool",
        durationMs: 42,
      }),
    ]);
  });

  it("keeps conversation memory mode from list payloads", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: null,
      conversations: [{
        id: "conv-memory",
        title: "Memory",
        updated_at: "2026-08-02T00:00:00.000Z",
        memory_mode: "polluted",
        memory_polluted: true,
        memory_pollution_sources: ["web_search", "mcp__github__search_issues"],
      }],
      active_conversation: null,
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    expect(useAppStore.getState().conversations).toEqual([
      expect.objectContaining({
        id: "conv-memory",
        memoryMode: "polluted",
        memoryPolluted: true,
        memoryPollutionSources: ["web_search", "mcp__github__search_issues"],
      }),
    ]);
  });

  it("conversation list switches to the active cached conversation through the shared hydration path", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-cached",
      session: {
        session_id: "session-list",
        active_conversation_id: "conv-cached",
        permission_mode: "auto",
        pending_approval_count: 0,
      },
      conversations: [{
        id: "conv-cached",
        title: "Cached",
        updated_at: "2026-01-04T00:00:00.000Z",
        workspace_root: "C:/repo-cached",
      }],
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-cached");
    expect(state.workingDirectory).toBe("C:/repo-cached");
    expect(state.messages).toEqual(state.conversationMessages["conv-cached"]);
    expect(state.runtimeSession).toMatchObject({
      session_id: "session-list",
      active_conversation_id: "conv-cached",
    });
    expect(state.permissionMode).toBe("auto");
  });

  it("conversation list does not rehydrate an already active conversation from a lagging snapshot", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    const currentMessages = [
      { id: "local-user", role: "user" as const, content: "current prompt", artifacts: [], timestamp: 1 },
      { id: "local-assistant", role: "assistant" as const, content: "current answer", artifacts: [], timestamp: 2 },
    ];
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [{
        id: "conv-active",
        title: "Active",
        updatedAt: "2026-01-04T00:00:00.000Z",
        workspaceRoot: "C:/repo-active",
      }],
      messages: currentMessages,
      conversationMessages: { "conv-active": currentMessages },
      conversationStreaming: { "conv-active": false },
      isStreaming: false,
      workingDirectory: "C:/repo-active",
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-active",
      conversations: [{
        id: "conv-active",
        title: "Active",
        updated_at: "2026-01-04T00:00:01.000Z",
        workspace_root: "C:/repo-active",
      }],
      active_conversation: {
        id: "conv-active",
        title: "Active",
        updated_at: "2026-01-04T00:00:01.000Z",
        workspace_root: "C:/repo-active",
        messages: [],
      },
      session: {
        session_id: "session-list",
        active_conversation_id: "conv-active",
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-active");
    expect(state.messages.map((message) => message.id)).toEqual(["local-user", "local-assistant"]);
    expect(state.workingDirectory).toBe("C:/repo-active");
  });

  it("treats conversation list as inventory and does not override a visible active owner", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    const currentMessages = [
      { id: "current-user", role: "user" as const, content: "current", artifacts: [], timestamp: 1 },
      { id: "current-assistant", role: "assistant" as const, content: "current answer", artifacts: [], timestamp: 2 },
    ];
    useAppStore.setState({
      conversationId: "conv-current",
      conversations: [
        { id: "conv-current", title: "Current", updatedAt: "2026-08-16T02:00:00Z" },
        { id: "conv-backend-old", title: "Backend old", updatedAt: "2026-08-15T02:00:00Z" },
      ],
      messages: currentMessages,
      conversationMessages: {
        "conv-current": currentMessages,
        "conv-backend-old": [{
          id: "old-assistant",
          role: "assistant",
          content: "old answer",
          artifacts: [],
          timestamp: 1,
        }],
      },
      conversationStreaming: {
        "conv-current": false,
        "conv-backend-old": false,
      },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-backend-old",
      snapshot_at: "2026-08-16T03:00:00Z",
      conversations: [
        { id: "conv-current", title: "Current", updated_at: "2026-08-16T02:00:00Z" },
        { id: "conv-backend-old", title: "Backend old", updated_at: "2026-08-15T02:00:00Z" },
      ],
      active_conversation: {
        id: "conv-backend-old",
        title: "Backend old",
        updated_at: "2026-08-15T02:00:00Z",
        messages: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-current");
    expect(state.messages.map((message) => message.id)).toEqual(["current-user", "current-assistant"]);
    expect(sendClientCommand).not.toHaveBeenCalledWith({
      type: "conversation.switch",
      conversation_id: "conv-current",
    });
  });

  it("conversation list with no active session clears chat but preserves open code context", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-stale",
      conversations: [{ id: "conv-stale", title: "Stale", updatedAt: "2026-01-01T00:00:00.000Z", workspaceRoot: "C:/repo" }],
      messages: [{ id: "m-stale", role: "assistant", content: "stale", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-stale": [{ id: "m-stale", role: "assistant", content: "stale", artifacts: [], timestamp: 1 }],
      },
      conversationStreaming: { "conv-stale": true },
      isStreaming: true,
      workingDirectory: "C:/repo",
      editorTabs: [{ path: "README.md", content: "", original: "", loading: false, error: null }],
      activeTabPath: "README.md",
      activeEditorPath: "README.md",
      contextUsage: { used: 80, limit: 100 },
      budgetBuckets: [{ name: "prompt", used: 80, limit: 0 }],
      totalBudgetPercent: 0.8,
      lastUsage: { input: 10, output: 5, cacheRead: 2, cacheWrite: 1 },
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: null,
      conversations: [],
      active_conversation: null,
      session: {
        session_id: "session-blank",
        active_conversation_id: null,
        permission_mode: "auto",
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.conversations).toEqual([]);
    expect(state.messages).toEqual([]);
    expect(state.conversationMessages["conv-stale"]).toBeUndefined();
    expect(state.conversationStreaming["conv-stale"]).toBeUndefined();
    expect(state.isStreaming).toBe(false);
    expect(state.workingDirectory).toBe("C:/repo");
    expect(state.editorTabs.map((tab) => tab.path)).toEqual(["README.md"]);
    expect(state.contextUsage).toBeNull();
    expect(state.budgetBuckets).toEqual([]);
    expect(state.totalBudgetPercent).toBe(0);
    expect(state.lastUsage).toBeNull();
    expect(state.runtimeSession).toMatchObject({ active_conversation_id: null });
  });

  it("conversation list retains archived inventory but clears it as the active view", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-archived",
      conversations: [{
        id: "conv-archived",
        title: "Archived",
        updatedAt: "2026-01-01T00:00:00.000Z",
        archived: true,
      }],
      messages: [{ id: "m-stale-archived", role: "assistant", content: "stale archived answer", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-archived": [{ id: "m-stale-archived", role: "assistant", content: "stale archived answer", artifacts: [], timestamp: 1 }],
      },
      conversationStreaming: { "conv-archived": true },
      isStreaming: true,
      workingDirectory: "C:/repo-archived",
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-archived",
      conversations: [{
        id: "conv-archived",
        title: "Archived",
        updated_at: "2026-01-01T00:00:00.000Z",
        workspace_root: "C:/repo-archived",
        archived: true,
      }],
      active_conversation: {
        id: "conv-archived",
        title: "Archived",
        updated_at: "2026-01-01T00:00:00.000Z",
        workspace_root: "C:/repo-archived",
        archived: true,
        messages: [{ id: "m-archived", role: "assistant", content: "archived transcript" }],
      },
      session: {
        session_id: "session-archived",
        active_conversation_id: "conv-archived",
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(["conv-archived"]);
    expect(state.conversations[0]?.archived).toBe(true);
    expect(state.messages).toEqual([]);
    expect(state.conversationMessages["conv-archived"]?.[0]?.id).toBe("m-stale-archived");
    expect(state.isStreaming).toBe(false);
    expect(state.workingDirectory).toBe("C:/repo-archived");
  });

  it("rejects older inventory revisions from the same durable epoch", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    useAppStore.setState({
      conversationId: "conv-active",
      conversationInventoryInstanceId: "epoch-a",
      conversationInventoryRevision: 10,
      conversations: [{
        id: "conv-active",
        title: "Newest title",
        updatedAt: "2026-08-16T10:00:00.000Z",
        revision: 5,
      }],
      messages: [],
      conversationMessages: { "conv-active": [] },
      conversationStreaming: { "conv-active": false },
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      inventory_instance_id: "epoch-a",
      inventory_revision: 9,
      active_conversation_id: "conv-active",
      conversations: [{
        id: "conv-active",
        title: "Stale title",
        updated_at: "2026-08-16T09:00:00.000Z",
        revision: 4,
      }],
    } as unknown as ServerEvent, buffers)).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationInventoryInstanceId).toBe("epoch-a");
    expect(state.conversationInventoryRevision).toBe(10);
    expect(state.conversations[0]?.title).toBe("Newest title");
    expect(state.conversations[0]?.revision).toBe(5);
  });

  it("accepts a lower revision from a new inventory epoch and drops old epoch state", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    useAppStore.setState({
      conversationId: "conv-active",
      conversationInventoryInstanceId: "epoch-old",
      conversationInventoryRevision: 100,
      conversations: [
        {
          id: "conv-active",
          title: "Old epoch title",
          updatedAt: "2026-08-16T10:00:00.000Z",
          revision: 99,
        },
        {
          id: "conv-deleted",
          title: "Deleted in new epoch",
          updatedAt: "2026-08-16T10:00:00.000Z",
          revision: 88,
        },
      ],
      messages: [],
      conversationMessages: {
        "conv-active": [],
        "conv-deleted": [{
          id: "old-message",
          role: "assistant",
          content: "old epoch",
          artifacts: [],
          timestamp: 1,
        }],
      },
      conversationStreaming: { "conv-active": false, "conv-deleted": false },
      pendingProviderProgress: {
        "conv-deleted\u0000assistant-deleted": [{
          type: "progress",
          id: "provider:connection:deleted:iteration-1",
          stage: "status",
          status: "running",
          message: "正在重连",
          timestamp: 1,
        }],
        "conv-active\u0000assistant-active": [{
          type: "progress",
          id: "provider:connection:active:iteration-1",
          stage: "status",
          status: "running",
          message: "正在重连",
          timestamp: 1,
        }],
      },
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      inventory_instance_id: "epoch-new",
      inventory_revision: 1,
      active_conversation_id: "conv-active",
      conversations: [{
        id: "conv-active",
        title: "New epoch authority",
        updated_at: "2026-08-15T00:00:00.000Z",
        revision: 1,
      }],
    } as unknown as ServerEvent, buffers)).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationInventoryInstanceId).toBe("epoch-new");
    expect(state.conversationInventoryRevision).toBe(1);
    expect(state.conversations).toHaveLength(1);
    expect(state.conversations[0]).toMatchObject({
      id: "conv-active",
      title: "New epoch authority",
      revision: 1,
    });
    expect(state.conversationMessages["conv-deleted"]).toBeUndefined();
    expect(state.pendingProviderProgress).toEqual({
      "conv-active\u0000assistant-active": [expect.objectContaining({
        id: "provider:connection:active:iteration-1",
      })],
    });
  });

  it("rejects legacy and half-versioned inventory after or before an epoch is known", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    useAppStore.setState({
      conversationId: "conv-active",
      conversationInventoryInstanceId: "epoch-known",
      conversationInventoryRevision: 4,
      conversations: [{
        id: "conv-active",
        title: "Keep",
        updatedAt: "2026-08-16T00:00:00.000Z",
        revision: 4,
      }],
      messages: [],
      conversationMessages: { "conv-active": [] },
      conversationStreaming: { "conv-active": false },
    });

    const staleList = {
      type: "conversation.list",
      active_conversation_id: "conv-active",
      conversations: [{
        id: "conv-active",
        title: "Must not replace",
        updated_at: "2026-08-17T00:00:00.000Z",
        revision: 5,
      }],
    };
    expect(handleSessionEvent(staleList as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().conversations[0]?.title).toBe("Keep");

    useAppStore.setState({
      conversationInventoryInstanceId: null,
      conversationInventoryRevision: 0,
    });
    expect(handleSessionEvent({
      ...staleList,
      inventory_revision: 1,
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(handleSessionEvent({
      ...staleList,
      inventory_instance_id: "epoch-half",
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().conversationInventoryInstanceId).toBeNull();
    expect(useAppStore.getState().conversationInventoryRevision).toBe(0);
    expect(useAppStore.getState().conversations[0]?.title).toBe("Keep");
  });

  it("cursor reset allows the following canonical switch to replace a newer old-epoch transcript", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    useAppStore.setState({
      conversationId: "conv-active",
      conversationInventoryInstanceId: "epoch-before-reset",
      conversationInventoryRevision: 50,
      conversations: [{
        id: "conv-active",
        title: "Before reset",
        updatedAt: "2026-08-16T10:00:00.000Z",
        revision: 50,
      }],
      messages: [
        { id: "old-1", role: "user", content: "old user", artifacts: [], timestamp: 1 },
        { id: "old-2", role: "assistant", content: "old answer", artifacts: [], timestamp: 2 },
      ],
      conversationMessages: {
        "conv-active": [
          { id: "old-1", role: "user", content: "old user", artifacts: [], timestamp: 1 },
          { id: "old-2", role: "assistant", content: "old answer", artifacts: [], timestamp: 2 },
        ],
      },
      conversationStreaming: { "conv-active": false },
    });

    expect(handleSessionEvent({
      type: "session.restored",
      cursor_reset: true,
      conversation_switched_follows: true,
      active_conversation_id: "conv-active",
      last_seq: 2,
      current_seq: 2,
      replayed_events: 0,
      session: {
        session_id: "session-reset",
        active_conversation_id: "conv-active",
        active_stream_conversation_ids: [],
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().conversationInventoryInstanceId).toBeNull();
    expect(useAppStore.getState().conversationInventoryRevision).toBe(0);

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-active",
      conversation: {
        id: "conv-active",
        title: "Restored authority",
        updated_at: "2026-08-15T00:00:00.000Z",
        revision: 1,
        messages: [],
      },
      session: {
        session_id: "session-reset",
        active_conversation_id: "conv-active",
        active_stream_conversation_ids: [],
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, buffers)).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversations[0]).toMatchObject({
      id: "conv-active",
      title: "Restored authority",
      revision: 1,
    });
    expect(state.messages).toEqual([]);
    expect(state.conversationMessages["conv-active"]).toEqual([]);
  });

  it("rejects stale or legacy goals and advances conversation revision with a newer goal", () => {
    const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
    useAppStore.setState({
      conversationId: "conv-active",
      activeGoal: {
        id: "goal-new",
        text: "Newest goal",
        status: "active",
        updatedAt: "2026-08-16T10:00:00.000Z",
      },
      conversations: [{
        id: "conv-active",
        title: "Task",
        updatedAt: "2026-08-16T10:00:00.000Z",
        revision: 10,
        goal: {
          id: "goal-new",
          text: "Newest goal",
          status: "active",
          updatedAt: "2026-08-16T10:00:00.000Z",
        },
      }],
    });

    const goalEvent = {
      type: "goal.updated",
      conversation_id: "conv-active",
      goal: {
        id: "goal-stale",
        text: "Stale goal",
        status: "active",
        updated_at: "2026-08-16T09:00:00.000Z",
      },
    };
    expect(handleSessionEvent({ ...goalEvent, revision: 9 } as unknown as ServerEvent, buffers)).toBe(true);
    expect(handleSessionEvent(goalEvent as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().activeGoal?.text).toBe("Newest goal");
    expect(useAppStore.getState().conversations[0]?.revision).toBe(10);

    expect(handleSessionEvent({
      ...goalEvent,
      revision: 11,
      goal: {
        id: "goal-latest",
        text: "Latest authoritative goal",
        status: "active",
        updated_at: "2026-08-16T11:00:00.000Z",
      },
    } as unknown as ServerEvent, buffers)).toBe(true);
    expect(useAppStore.getState().activeGoal?.text).toBe("Latest authoritative goal");
    expect(useAppStore.getState().conversations[0]?.goal?.text).toBe("Latest authoritative goal");
    expect(useAppStore.getState().conversations[0]?.revision).toBe(11);
  });

  it("conversation switched clears stale chat if the backend tries to switch to an archived conversation", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-01-01T00:00:00.000Z" },
        { id: "conv-archived", title: "Archived", updatedAt: "2026-01-02T00:00:00.000Z", archived: true },
      ],
      messages: [{ id: "m-active", role: "assistant", content: "active answer", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-active": [{ id: "m-active", role: "assistant", content: "active answer", artifacts: [], timestamp: 1 }],
      },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-archived",
      conversation: {
        id: "conv-archived",
        title: "Archived",
        updated_at: "2026-01-02T00:00:00.000Z",
        archived: true,
        messages: [{ id: "m-archived", role: "assistant", content: "archived answer" }],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.messages).toEqual([]);
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(["conv-active", "conv-archived"]);
  });

  it("conversation switch clears token and context usage before the target reports fresh values", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      conversations: [
        { id: "conv-active", title: "Active", updatedAt: "2026-01-01T00:00:00.000Z" },
        { id: "conv-next", title: "Next", updatedAt: "2026-01-02T00:00:00.000Z" },
      ],
      messages: [{ id: "m-active", role: "assistant", content: "active", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-active": [{ id: "m-active", role: "assistant", content: "active", artifacts: [], timestamp: 1 }],
        "conv-next": [{ id: "m-next", role: "assistant", content: "next", artifacts: [], timestamp: 2 }],
      },
      conversationStreaming: { "conv-active": false, "conv-next": false },
      contextUsage: { used: 700, limit: 1000 },
      budgetBuckets: [{ name: "prompt", used: 700, limit: 0 }],
      totalBudgetPercent: 0.7,
      lastUsage: { input: 300, output: 50, cacheRead: 10, cacheWrite: 0 },
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-next",
      conversation: {
        id: "conv-next",
        title: "Next",
        updated_at: "2026-01-02T00:00:00.000Z",
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-next");
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(["conv-active", "conv-next"]);
    expect(state.messages.map((message) => message.id)).toEqual(["m-next"]);
    expect(state.contextUsage).toBeNull();
    expect(state.budgetBuckets).toEqual([]);
    expect(state.totalBudgetPercent).toBe(0);
    expect(state.lastUsage).toBeNull();
  });

  it("treats a canonical list as authoritative over obsolete optimistic state", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv_local_new",
      conversations: [{
        id: "conv_local_new",
        title: "New chat",
        updatedAt: "2026-01-01T00:00:00.000Z",
      }],
      messages: [{ id: "local-user", role: "user", content: "first message", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv_local_new": [{ id: "local-user", role: "user", content: "first message", artifacts: [], timestamp: 1 }],
      },
      conversationStreaming: { "conv_local_new": false },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: null,
      conversations: [{
        id: "conv-history",
        title: "History",
        updated_at: "2026-01-02T00:00:00.000Z",
      }],
      active_conversation: null,
      session: {
        session_id: "session-list",
        active_conversation_id: null,
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-history");
    expect(state.messages).toEqual([]);
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(["conv-history"]);
    expect(state.conversationMessages["conv_local_new"]).toBeUndefined();
  });

  it("conversation switched restores cached messages and goal when transcript is absent", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-cached",
      conversation: {
        id: "conv-cached",
        title: "Cached",
        updated_at: "2026-01-06T00:00:00.000Z",
        workspace_root: "C:/repo-cached",
        goal: {
          id: "goal-1",
          text: "对标 MiniCode 桌面端",
          status: "active",
          source: "user",
        },
      },
      session: {
        session_id: "session-switched",
        active_conversation_id: "conv-cached",
        permission_mode: "confirm",
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-cached");
    expect(state.workingDirectory).toBe("C:/repo-cached");
    expect(state.messages).toEqual(state.conversationMessages["conv-cached"]);
    expect(state.activeGoal).toMatchObject({ id: "goal-1", text: "对标 MiniCode 桌面端" });
    expect(state.conversations[0]).toMatchObject({
      id: "conv-cached",
      goal: expect.objectContaining({ id: "goal-1", text: "对标 MiniCode 桌面端" }),
    });
    expect(state.runtimeSession).toMatchObject({
      session_id: "session-switched",
      active_conversation_id: "conv-cached",
    });
    expect(state.permissionMode).toBe("confirm");
  });

  // Contract, not a bug: an empty workspace_root/worktree_path on the wire is
  // faithful, not a missing field. The backend's own
  // `_switch_workspace_for_conversation` (backend/ws/command_handlers.py) calls
  // `_clear_workspace_runtime()` for a conversation with no workspace, and
  // `handle_conversation_create` does the same, so the renderer has to clear too
  // or its workspace diverges from the session's. Editor tabs are persisted per
  // workspace, so switching back restores them.
  it("conversation switched mirrors the backend clearing the workspace for a workspace-less conversation", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      workingDirectory: "C:/repo-open",
      workspaceGit: { branch: "main", dirty: false, ahead: 0, behind: 0 },
    } as never);

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-no-workspace",
      conversation: {
        id: "conv-no-workspace",
        title: "No workspace",
        updated_at: "2026-01-06T00:00:00.000Z",
        workspace_root: "",
        worktree_path: "",
        transcript: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-no-workspace");
    expect(state.workingDirectory).toBe("");
    expect(state.workspaceGit).toBeNull();
  });

  it("conversation switched adopts a workspace the conversation does declare", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({ workingDirectory: "C:/repo-open" });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-own-workspace",
      conversation: {
        id: "conv-own-workspace",
        title: "Own workspace",
        updated_at: "2026-01-06T00:00:00.000Z",
        workspace_root: "C:/repo-other",
        worktree_path: "",
        transcript: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    expect(useAppStore.getState().workingDirectory).toBe("C:/repo-other");
  });

  it("hydrates plan, todos, subagents, and progress from conversation snapshot", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      plan: null,
      todos: [],
      subagents: [],
      agentProgress: [],
      conversationAgentStates: {},
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-agent-state",
      conversation: {
        id: "conv-agent-state",
        title: "Agent state",
        updated_at: "2026-01-07T00:00:00.000Z",
        context_snapshot: {
          context_ledger: {
            estimated_tokens: 900,
            actual_tokens: 960,
            compaction_count: 1,
            entries: [{
              category: "history",
              label: "History",
              estimated_tokens: 640,
              item_count: 5,
              source_count: 0,
              sources: [],
            }],
          },
          ui_agent_state: {
            plan: {
              threadId: "conv-agent-state",
              turnId: "turn-agent-state",
              plan: [
                { step: "Inspect flow", status: "completed" },
                { step: "Patch hydrate", status: "in_progress" },
              ],
            },
            todos: [
              { id: "todo-1", content: "Persist tasks", activeForm: "Persisting tasks", status: "in_progress" },
            ],
            subagents: [
              { id: "sa-1", role: "reviewer", status: "running", summary: "Reviewing diff" },
              { id: "wf-pending", role: "workflow step", status: "pending", summary: "Waiting to launch" },
              { id: "wf-blocked", role: "workflow step", status: "blocked", summary: "Waiting on dependency" },
              {
                id: "sa-partial",
                role: "explore",
                status: "partial",
                result_content: "Found two likely causes",
                result_error: "deadline reached",
                activity_log: ["Inspected runner"],
                termination_reason: "deadline_exceeded",
              },
            ],
            agentProgress: [
              {
                type: "progress",
                id: "progress-1",
                stage: "planning",
                status: "running",
                message: "Updating plan",
                timestamp: 1000,
              },
              {
                type: "progress",
                id: "subagent:sa-1",
                stage: "status",
                phase: "subagent",
                status: "running",
                message: "Reviewing diff",
                visibility: "compact",
                timestamp: 1100,
              },
              {
                type: "progress",
                id: "cache:provider.prompt:sig",
                stage: "status",
                phase: "cache",
                status: "completed",
                message: "Cache hit: provider.prompt",
                visibility: "debug",
                timestamp: 1300,
              },
            ],
          },
        },
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-agent-state");
    expect(state.plan?.threadId).toBe("conv-agent-state");
    expect(state.plan?.turnId).toBe("turn-agent-state");
    expect(state.plan?.plan[1]).toEqual({ step: "Patch hydrate", status: "in_progress" });
    expect(state.todos).toEqual([
      expect.objectContaining({ id: "todo-1", status: "in_progress" }),
    ]);
    expect(state.subagents).toEqual([
      expect.objectContaining({ id: "sa-1", status: "running" }),
      expect.objectContaining({ id: "wf-pending", status: "pending" }),
      expect.objectContaining({ id: "wf-blocked", status: "blocked" }),
      expect.objectContaining({
        id: "sa-partial",
        status: "partial",
        resultContent: "Found two likely causes",
        resultError: "deadline reached",
        activityLog: ["Inspected runner"],
        terminationReason: "deadline_exceeded",
      }),
    ]);
    expect(state.agentProgress[0]).toMatchObject({
      id: "progress-1",
      conversationId: "conv-agent-state",
    });
    expect(state.agentProgress.map((entry) => entry.phase)).toEqual([
      undefined,
      "subagent",
      "cache",
    ]);
    expect(state.conversationAgentStates["conv-agent-state"].todos[0].id).toBe("todo-1");
    expect(state.contextUsage?.ledger).toMatchObject({
      estimated_tokens: 900,
      actual_tokens: 960,
      compaction_count: 1,
    });
    expect(state.contextUsage?.ledger?.entries[0]?.category).toBe("history");
  });

  it("does not let a stale agent-state snapshot overwrite an in-flight local turn", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-agent-state",
      messages: [
        { id: "user-cf", role: "user", content: "换个游戏吧 我要完cf", artifacts: [], timestamp: 1 },
        {
          id: "assistant-cf",
          role: "assistant",
          content: "",
          blocks: [],
          artifacts: [],
          timestamp: 2,
          isStreaming: true,
        },
      ],
      conversationMessages: {},
      conversationStreaming: { "conv-agent-state": true },
      plan: null,
      todos: [],
      subagents: [],
      agentProgress: [],
      conversationAgentStates: {
        "conv-agent-state": {
          plan: null,
          todos: [],
          subagents: [],
          agentProgress: [],
        },
      },
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-agent-state",
      conversation: {
        id: "conv-agent-state",
        title: "Agent state",
        updated_at: "2026-01-07T00:00:00.000Z",
        context_snapshot: {
          ui_agent_state: {
            plan: {
              threadId: "conv-agent-state",
              turnId: "turn-angry-birds",
              plan: [
                { step: "用单文件 HTML 实现愤怒的小鸟游戏", status: "in_progress" },
              ],
            },
            todos: [
              {
                id: "todo-angry-birds",
                content: "编写愤怒的小鸟 HTML 游戏",
                activeForm: "正在编写愤怒的小鸟 HTML 游戏",
                status: "in_progress",
              },
            ],
            subagents: [],
            agentProgress: [],
          },
        },
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.plan).toBeNull();
    expect(state.todos).toEqual([]);
    expect(state.conversationAgentStates["conv-agent-state"]).toMatchObject({
      plan: null,
      todos: [],
    });
    expect(state.messages.at(-1)).toMatchObject({
      id: "assistant-cf",
      isStreaming: true,
    });
  });

  it("conversation switched keeps an in-flight streaming assistant when transcript lags behind", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [{ id: "active-user", role: "user", content: "active", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-cached": [
          { id: "cached-user", role: "user", content: "今天北京天气如何", artifacts: [], timestamp: 1 },
          {
            id: "cached-assistant-streaming",
            role: "assistant",
            content: "北京今天",
            blocks: [
              { type: "tool_call", record: { id: "search-1", name: "web_search", args: {}, status: "success", startedAt: 1 } },
              { type: "text", content: "北京今天" },
            ],
            artifacts: [],
            timestamp: 2,
            isStreaming: true,
          },
        ],
      },
      conversationStreaming: {
        "conv-cached": true,
      },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-cached",
      conversation: {
        id: "conv-cached",
        title: "北京天气",
        updated_at: "2026-01-06T00:00:00.000Z",
        messages: [
          { id: "cached-user", role: "user", content: "今天北京天气如何", timestamp: 1 },
        ],
      },
      session: {
        active_stream_conversation_ids: ["conv-cached"],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-cached");
    expect(state.isStreaming).toBe(true);
    expect(state.messages.map((message) => message.id)).toEqual([
      "cached-user",
      "cached-assistant-streaming",
    ]);
    expect(state.messages[1]).toMatchObject({
      role: "assistant",
      content: "北京今天",
      isStreaming: true,
    });
    expect(getToolCallsFromMessage(state.messages[1])).toHaveLength(1);
  });

  it("conversation switched replaces stale streaming when the backend has no active stream", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [],
      conversationMessages: {
        "conv-finished": [{
          id: "assistant-finished",
          role: "assistant",
          content: "旧草稿",
          blocks: [{ type: "text", content: "旧草稿" }],
          artifacts: [],
          timestamp: 1,
          isStreaming: true,
        }],
      },
      conversationStreaming: { "conv-finished": true },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-finished",
      conversation: {
        id: "conv-finished",
        title: "Finished",
        updated_at: "2026-01-06T00:00:00.000Z",
        messages: [{
          id: "assistant-finished",
          role: "assistant",
          content: "最终答案",
          timestamp: 2,
          terminal_status: "completed",
        }],
      },
      session: {
        active_stream_conversation_ids: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.isStreaming).toBe(false);
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      id: "assistant-finished",
      content: "最终答案",
      terminalStatus: "completed",
    });
    expect(state.messages[0].isStreaming).toBeFalsy();
  });

  it("conversation list falls back to a visible session instead of blanking the UI", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-deleted",
      conversations: [{ id: "conv-deleted", title: "Deleted", updatedAt: "2026-01-01T00:00:00.000Z" }],
      messages: [{ id: "m-deleted", role: "assistant", content: "deleted", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-deleted": [{ id: "m-deleted", role: "assistant", content: "deleted", artifacts: [], timestamp: 1 }],
        "conv-next": [{ id: "m-next", role: "assistant", content: "next cached", artifacts: [], timestamp: 2 }],
      },
      conversationStreaming: { "conv-deleted": false, "conv-next": false },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: null,
      conversations: [{
        id: "conv-next",
        title: "Next",
        updated_at: "2026-01-02T00:00:00.000Z",
        workspace_root: "C:/repo-next",
      }],
      active_conversation: null,
      session: {
        session_id: "session-fallback",
        active_conversation_id: null,
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-next");
    expect(state.messages.map((message) => message.id)).toEqual(["m-next"]);
    expect(state.conversationMessages["conv-deleted"]).toBeUndefined();
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "conversation.switch",
      conversation_id: "conv-next",
    });
  });

  it("does not switch the backend back to the old task while a newly created task activation is in flight", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-old",
      conversations: [{ id: "conv-old", title: "Old", updatedAt: "2026-01-01T00:00:00.000Z" }],
      messages: [{ id: "old-message", role: "assistant", content: "old", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-old": [{ id: "old-message", role: "assistant", content: "old", artifacts: [], timestamp: 1 }],
      },
      conversationStreaming: { "conv-old": false },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-new",
      conversations: [
        { id: "conv-new", title: "New chat", updated_at: "2026-01-02T00:00:00.000Z" },
        { id: "conv-old", title: "Old", updated_at: "2026-01-01T00:00:00.000Z" },
      ],
      active_conversation: {
        id: "conv-new",
        title: "New chat",
        updated_at: "2026-01-02T00:00:00.000Z",
        messages: [],
      },
      session: {
        session_id: "session-create-race",
        active_conversation_id: "conv-new",
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    expect(useAppStore.getState().conversationId).toBe("conv-old");
    expect(sendClientCommand).not.toHaveBeenCalledWith({
      type: "conversation.switch",
      conversation_id: "conv-old",
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-new",
      conversation: {
        id: "conv-new",
        title: "New chat",
        updated_at: "2026-01-02T00:00:00.000Z",
        messages: [],
      },
      session: {
        session_id: "session-create-race",
        active_conversation_id: "conv-new",
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    expect(useAppStore.getState().conversationId).toBe("conv-new");
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it("applies the backend active id instead of preserving obsolete optimistic state", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-local-new",
      conversations: [
        { id: "conv-local-new", title: "New chat", updatedAt: "2026-01-03T00:00:00.000Z" },
        { id: "conv-old", title: "Old running", updatedAt: "2026-01-01T00:00:00.000Z" },
      ],
      messages: [],
      conversationMessages: {
        "conv-local-new": [],
        "conv-old": [
          { id: "old-user", role: "user", content: "write html", artifacts: [], timestamp: 1 },
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
      },
      conversationStreaming: {
        "conv-local-new": false,
        "conv-old": true,
      },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-old",
      conversations: [{
        id: "conv-old",
        title: "Old running",
        updated_at: "2026-01-01T00:00:00.000Z",
      }],
      active_conversation: {
        id: "conv-old",
        title: "Old running",
        updated_at: "2026-01-01T00:00:00.000Z",
        messages: [],
      },
      session: {
        session_id: "session-stale",
        active_conversation_id: "conv-old",
        pending_approval_count: 0,
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-old");
    expect(state.messages.map((message) => message.id)).toEqual(["old-user", "old-assistant"]);
    expect(state.isStreaming).toBe(true);
    expect(state.conversationStreaming["conv-old"]).toBe(true);
    expect(state.conversations.map((conversation) => conversation.id)).toEqual(["conv-old"]);
  });

  it("conversation switched does not erase cached messages when backend sends an empty lagging transcript", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-active",
      messages: [{ id: "active-user", role: "user", content: "active", artifacts: [], timestamp: 1 }],
      conversationMessages: {
        "conv-cached": [
          { id: "cached-user", role: "user", content: "写一个 README", artifacts: [], timestamp: 1 },
          { id: "cached-assistant", role: "assistant", content: "README 已写好。", artifacts: [], timestamp: 2 },
        ],
      },
      conversationStreaming: {
        "conv-cached": false,
      },
      isStreaming: false,
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-cached",
      conversation: {
        id: "conv-cached",
        title: "README",
        updated_at: "2026-01-06T00:00:00.000Z",
        workspace_root: "C:/repo-cached",
        messages: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversationId).toBe("conv-cached");
    expect(state.workingDirectory).toBe("C:/repo-cached");
    expect(state.messages.map((message) => message.id)).toEqual([
      "cached-user",
      "cached-assistant",
    ]);
  });

  it("goal updates only affect the targeted inactive conversation", () => {
    useAppStore.setState({
      conversations: [
        {
          id: "conv-active",
          title: "Active",
          updatedAt: "2026-01-01T00:00:00.000Z",
          goal: { text: "keep current", status: "active" },
        },
        {
          id: "conv-cached",
          title: "Cached",
          updatedAt: "2026-01-04T00:00:00.000Z",
        },
      ],
      activeGoal: { text: "keep current", status: "active" },
    });
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "goal.updated",
      conversation_id: "conv-cached",
      goal: {
        id: "goal-2",
        text: "缓存会话目标",
        status: "active",
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.activeGoal).toEqual({ text: "keep current", status: "active" });
    expect(state.conversations.find((conversation) => conversation.id === "conv-cached")?.goal)
      .toMatchObject({ id: "goal-2", text: "缓存会话目标" });
  });

  it("hydrates skill and command catalogs from session capabilities", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();

    expect(handleSessionEvent({
      type: "session.synced",
      session: {
        session_id: "session-1",
        capabilities: {
          skills: [{
            name: "frontend-dev",
            description: "Frontend workflow",
            display_name: "Frontend Dev",
            source_level: "project",
            allow_implicit_invocation: true,
          }],
          composer_commands: [{
            name: "automation",
            command: "automation",
            label: "/automation",
            description: "Open automations",
            type: "local",
          }],
        },
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.availableSkills).toMatchObject([{
      name: "frontend-dev",
      display_name: "Frontend Dev",
      source_level: "project",
      allow_implicit_invocation: true,
    }]);
    expect(state.slashCommands).toMatchObject([{
      name: "automation",
      command: "automation",
      label: "/automation",
      description: "Open automations",
      type: "local",
    }]);
  });

  it("does not let an older switched snapshot regress metadata, workspace, goal, or an in-flight message", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-stale",
      conversations: [{
        id: "conv-stale",
        title: "Fresh title",
        updatedAt: "2026-01-10T00:00:00.000Z",
        workspaceRoot: "C:/fresh",
        goal: {
          text: "Fresh goal",
          status: "active",
          updatedAt: "2026-01-10T00:00:00.000Z",
        },
      }],
      activeGoal: {
        text: "Fresh goal",
        status: "active",
        updatedAt: "2026-01-10T00:00:00.000Z",
      },
      workingDirectory: "C:/fresh",
      messages: [{
        id: "assistant-live",
        role: "assistant",
        content: "fresh streamed answer",
        blocks: [{ type: "text", content: "fresh streamed answer" }],
        artifacts: [],
        timestamp: 20,
        isStreaming: true,
        turnId: "turn-fresh",
      }],
      conversationMessages: {},
      conversationStreaming: { "conv-stale": true },
      isStreaming: true,
      agentProgress: [{
        type: "progress",
        id: "provider:fresh",
        stage: "tool",
        status: "running",
        message: "Fresh Provider activity",
        timestamp: 20,
        conversationId: "conv-stale",
      }],
    });

    expect(handleSessionEvent({
      type: "conversation.switched",
      conversation_id: "conv-stale",
      conversation: {
        id: "conv-stale",
        title: "Old title",
        updated_at: "2026-01-09T00:00:00.000Z",
        workspace_root: "C:/old",
        goal: {
          text: "Old goal",
          status: "active",
          updated_at: "2026-01-09T00:00:00.000Z",
        },
        messages: [{
          id: "assistant-live",
          role: "assistant",
          content: "old partial answer",
          timestamp: 10,
        }],
        context_snapshot: {
          ui_agent_state: {
            plan: null,
            todos: [],
            subagents: [],
            agentProgress: [{
              type: "progress",
              id: "provider:old",
              stage: "tool",
              status: "completed",
              message: "Old Provider activity",
              timestamp: 10,
            }],
          },
        },
      },
      session: { active_stream_conversation_ids: ["conv-stale"] },
      snapshot_at: "2026-01-09T00:00:00.000Z",
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    const state = useAppStore.getState();
    expect(state.conversations[0]).toMatchObject({
      title: "Fresh title",
      workspaceRoot: "C:/fresh",
      goal: { text: "Fresh goal" },
    });
    expect(state.workingDirectory).toBe("C:/fresh");
    expect(state.activeGoal).toMatchObject({ text: "Fresh goal" });
    expect(state.messages[0]).toMatchObject({
      id: "assistant-live",
      content: "fresh streamed answer",
      isStreaming: true,
      turnId: "turn-fresh",
    });
    expect(state.agentProgress).toEqual([
      expect.objectContaining({
        id: "provider:fresh",
        message: "Fresh Provider activity",
      }),
    ]);
  });

  it("preserves conversations and metadata newer than a stale list snapshot", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-current",
      conversations: [
        {
          id: "conv-current",
          title: "Fresh current",
          updatedAt: "2026-01-10T00:00:00.000Z",
        },
        {
          id: "conv-created-later",
          title: "Created later",
          updatedAt: "2026-01-11T00:00:00.000Z",
        },
      ],
      conversationMessages: {
        "conv-current": [],
        "conv-created-later": [],
      },
    });

    expect(handleSessionEvent({
      type: "conversation.list",
      active_conversation_id: "conv-current",
      snapshot_at: "2026-01-09T00:00:00.000Z",
      conversations: [{
        id: "conv-current",
        title: "Old current",
        updated_at: "2026-01-08T00:00:00.000Z",
      }],
      active_conversation: {
        id: "conv-current",
        title: "Old current",
        updated_at: "2026-01-08T00:00:00.000Z",
        messages: [],
      },
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    expect(useAppStore.getState().conversations).toEqual([
      expect.objectContaining({ id: "conv-current", title: "Fresh current" }),
      expect.objectContaining({ id: "conv-created-later", title: "Created later" }),
    ]);
    expect(useAppStore.getState().conversationMessages["conv-created-later"]).toEqual([]);
  });

  it("does not let an old clear-goal tombstone erase a newer goal", () => {
    const textStreamBuffer = makeBuffer();
    const thinkingStreamBuffer = makeBuffer();
    useAppStore.setState({
      conversationId: "conv-goal",
      conversations: [{
        id: "conv-goal",
        title: "Goal",
        updatedAt: "2026-01-10T00:00:00.000Z",
        goal: {
          text: "Keep the newer goal",
          status: "active",
          updatedAt: "2026-01-10T00:00:00.000Z",
        },
      }],
      activeGoal: {
        text: "Keep the newer goal",
        status: "active",
        updatedAt: "2026-01-10T00:00:00.000Z",
      },
    });

    expect(handleSessionEvent({
      type: "goal.updated",
      conversation_id: "conv-goal",
      goal: {},
      updated_at: "2026-01-09T00:00:00.000Z",
      replayed: true,
    } as unknown as ServerEvent, { textStreamBuffer, thinkingStreamBuffer })).toBe(true);

    expect(useAppStore.getState().activeGoal).toMatchObject({ text: "Keep the newer goal" });
    expect(useAppStore.getState().conversations[0].goal).toMatchObject({
      text: "Keep the newer goal",
    });
  });
});
