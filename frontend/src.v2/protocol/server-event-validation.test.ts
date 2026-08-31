import { afterEach, describe, expect, it, vi } from "vitest";
import { normalizeInboundServerEvent } from "./server-event-validation";

const checkpointEvent = (overrides: Record<string, unknown> = {}) => ({
  type: "checkpoint.created",
  id: "cp-1",
  conversation_id: "conversation-1",
  session_id: "session-1",
  tool_call_id: "tool-1",
  tool_name: "write_file",
  workspace_root: "C:\\workspace",
  paths: ["src/app.ts"],
  created_at: "2026-08-15T01:02:03Z",
  metadata: { reason: "before_write" },
  ...overrides,
});

const commandCatalogEntry = (overrides: Record<string, unknown> = {}) => ({
  id: "extension:review",
  name: "review",
  command: "review",
  label: "/review",
  description: "Review the current workspace changes.",
  type: "template",
  kind: "prompt",
  source: "extension",
  enabled: true,
  availability: {
    kind: "available",
    scope: "conversation",
    reason: "Extension is enabled for this workspace.",
  },
  args: [{ value: "focus", description: "Optional review focus." }],
  extension_path: "C:\\extensions\\review",
  source_path: "C:\\extensions\\review\\commands\\review.md",
  template: "Review $ARGUMENTS",
  search_text: "review changes",
  argument_hint: "[focus]",
  argument_names: ["focus"],
  base_dir: "C:\\extensions\\review",
  is_skill_file: false,
  ...overrides,
});

const workspaceImportedEvent = (overrides: Record<string, unknown> = {}) => ({
  type: "workspace.imported",
  conversation_id: "conversation-1",
  workspace_root: "C:\\workspace",
  request_id: "workspace-request-1",
  project: {
    root_path: "C:\\workspace",
    project_type: "python",
    name: "Workspace",
    description: "Release audit workspace",
    file_count: 42,
    total_size: 123_456,
    has_project_instructions: false,
    index_truncated: false,
  },
  summary: "Python project with 42 files",
  file_count: 42,
  ...overrides,
});

describe("normalizeInboundServerEvent", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("accepts known server events without warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const event = normalizeInboundServerEvent({ type: "pong", seq: 1, event_id: "session:1" });
    const checkpoint = normalizeInboundServerEvent(checkpointEvent());
    const replay = normalizeInboundServerEvent({
      type: "session.replay",
      last_seq: 0,
      current_seq: 0,
      replayed_events: 0,
      events: [],
    });
    const parentNotifications = normalizeInboundServerEvent({
      type: "parent.notifications",
      count: 3,
      parent_run_id: "run-parent",
      conversation_id: "conversation-1",
    });

    expect(event).toEqual({ type: "pong", seq: 1, event_id: "session:1" });
    expect(checkpoint).toMatchObject({ type: "checkpoint.created", id: "cp-1" });
    expect(replay).toMatchObject({ type: "session.replay", events: [] });
    expect(parentNotifications).toMatchObject({
      type: "parent.notifications",
      count: 3,
      parent_run_id: "run-parent",
      conversation_id: "conversation-1",
    });
    expect(warn).not.toHaveBeenCalled();
  });

  it("validates live and replayed legacy raster image events by exact shape and file signature", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const rasterImages = [
      ["image/png", "iVBORw0KGgo="],
      ["image/jpeg", "/9j/"],
      ["image/gif", "R0lGODlh"],
      ["image/webp", "UklGRgAAAABXRUJQ"],
    ] as const;

    for (const [mediaType, imageData] of rasterImages) {
      expect(normalizeInboundServerEvent({
        type: "image_chunk",
        conversation_id: "conversation-1",
        message_id: "assistant-1",
        media_type: mediaType,
        image_data: imageData,
        image_data_size: imageData.length,
      })).toMatchObject({ media_type: mediaType, image_data: imageData });
    }
    expect(normalizeInboundServerEvent({
      type: "image_chunk",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      media_type: "image/png",
      image_data_omitted: true,
      image_data_size: 4_096,
    })).toMatchObject({ image_data_omitted: true, image_data_size: 4_096 });
    expect(warn).not.toHaveBeenCalled();

    const invalidEvents = [
      {
        type: "image_chunk",
        conversation_id: "conversation-1",
        message_id: "assistant-1",
        media_type: "image/png",
        image_data: "not base64!",
      },
      {
        type: "image_chunk",
        conversation_id: "conversation-1",
        message_id: "assistant-1",
        media_type: "image/jpeg",
        image_data: "iVBORw0KGgo=",
      },
      {
        type: "image_chunk",
        conversation_id: "conversation-1",
        message_id: "assistant-1",
        media_type: "image/svg+xml",
        image_data: "PHN2Zz48L3N2Zz4=",
      },
      {
        type: "image_chunk",
        conversation_id: "conversation-1",
        message_id: "assistant-1",
        media_type: "image/png",
        image_data: "iVBORw0KGgo=",
        image_data_omitted: false,
      },
      {
        type: "image_chunk",
        conversation_id: "conversation-1",
        message_id: "assistant-1",
        media_type: "image/png",
        image_data: "iVBORw0KGgo=",
        image_data_omitted: true,
        image_data_size: 12,
      },
      {
        type: "image_chunk",
        conversation_id: "conversation-1",
        message_id: "assistant-1",
        media_type: "image/png",
        image_data: "iVBORw0KGgo=",
        image_data_size: 1,
      },
    ];
    for (const event of invalidEvents) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
    expect(warn).toHaveBeenCalled();
  });

  it("validates exact command output ownership, stream and tool identity", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const event = {
      type: "command_output_chunk",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      turn_id: "turn-1",
      id: "tool-1",
      tool_call_id: "tool-1",
      stream: "stdout",
      content: "build output",
    };
    expect(normalizeInboundServerEvent(event)).toMatchObject(event);
    expect(normalizeInboundServerEvent({ ...event, stream: "stderr" })).toMatchObject({ stream: "stderr" });
    expect(normalizeInboundServerEvent({ ...event, stream: "combined" })).toBeNull();
    expect(normalizeInboundServerEvent({ ...event, tool_call_id: "tool-2" })).toBeNull();
    expect(normalizeInboundServerEvent({ ...event, conversation_id: " " })).toBeNull();
    expect(normalizeInboundServerEvent({ ...event, message_id: " " })).toBeNull();
  });

  it("validates complete command catalogs and rejects ambiguous or oversized entries", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const event = {
      type: "commands.list",
      conversation_id: "conversation-1",
      request_id: "commands-request-1",
      commands: [commandCatalogEntry()],
    };
    expect(normalizeInboundServerEvent(event)).toMatchObject(event);
    expect(normalizeInboundServerEvent({ ...event, conversation_id: null })).toMatchObject({ conversation_id: null });
    expect(normalizeInboundServerEvent({ ...event, commands: [commandCatalogEntry({ availability: {} })] })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...event,
      commands: [commandCatalogEntry({
        args: [
          { value: "focus", description: "One" },
          { value: "FOCUS", description: "Duplicate" },
        ],
      })],
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...event,
      commands: Array.from({ length: 2_049 }, () => commandCatalogEntry()),
    })).toBeNull();
    const missingOwner = { ...event } as Record<string, unknown>;
    delete missingOwner.conversation_id;
    expect(normalizeInboundServerEvent(missingOwner)).toBeNull();
  });

  it("keeps pong transport-only and validates informative notices and workspace imports", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(normalizeInboundServerEvent({ type: "pong", seq: 7, event_id: "session:7" })).toBeTruthy();
    expect(normalizeInboundServerEvent({ type: "pong", latency_ms: 12 })).toBeNull();

    expect(normalizeInboundServerEvent({
      type: "system_notice",
      conversation_id: "conversation-1",
      content: "Index refreshed",
    })).toBeTruthy();
    expect(normalizeInboundServerEvent({
      type: "system_notice",
      conversation_id: "conversation-1",
      title: "Resumed from checkpoint",
      message: "Continuing from iteration 3.",
      data: { iteration: 3 },
    })).toBeTruthy();
    expect(normalizeInboundServerEvent({
      type: "system_notice",
      conversation_id: "conversation-1",
      title: "Title without a message",
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "system_notice",
      conversation_id: "conversation-1",
      content: " ",
    })).toBeNull();

    expect(normalizeInboundServerEvent(workspaceImportedEvent())).toMatchObject({
      type: "workspace.imported",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
      file_count: 42,
    });
    expect(normalizeInboundServerEvent(workspaceImportedEvent({
      workspace_root: "C:\\another-workspace",
    }))).toBeNull();
    expect(normalizeInboundServerEvent(workspaceImportedEvent({
      file_count: 41,
    }))).toBeNull();
  });

  it("requires exact parent notification ownership and positive delivery evidence", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const event = {
      type: "parent.notifications",
      conversation_id: "conversation-1",
      parent_run_id: "run-parent",
      count: 3,
    };
    expect(normalizeInboundServerEvent(event)).toMatchObject(event);
    expect(normalizeInboundServerEvent({ ...event, count: 0 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...event, count: 1.5 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...event, parent_run_id: " " })).toBeNull();
    const unowned = { ...event } as Record<string, unknown>;
    delete unowned.conversation_id;
    expect(normalizeInboundServerEvent(unowned)).toBeNull();
  });

  it("accepts RFC3339 file-change timestamps and rejects legacy numeric timestamps", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const timestamp = "2026-08-09T08:00:00Z";

    const event = normalizeInboundServerEvent({
      type: "file.changed",
      conversation_id: "conv",
      workspace_root: "C:\\repo",
      path: "src/app.ts",
      event: "modified",
      timestamp,
    });

    expect(event).toMatchObject({ type: "file.changed", timestamp });
    expect(warn).not.toHaveBeenCalled();

    expect(normalizeInboundServerEvent({
      type: "file.changed",
      conversation_id: "conv",
      workspace_root: "C:\\repo",
      path: "src/app.ts",
      event: "modified",
      timestamp: 1786257304404,
    })).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping server event with invalid timestamp",
      "file.changed",
      1786257304404,
    );
  });

  it("accepts subagent progress events from the backend contract", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const event = normalizeInboundServerEvent({
      type: "subagent.progress",
      subagent_id: "subagent-1",
      iteration: 1,
      tool_name: "read_file",
    });

    expect(event).toMatchObject({ type: "subagent.progress", subagent_id: "subagent-1" });
    expect(warn).not.toHaveBeenCalled();
  });

  it("accepts image-generation and cache agent progress stages from the backend contract", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const base = {
      type: "agent.progress",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      id: "provider:image-generation-1",
      status: "running",
      message: "正在生成图像",
      visibility: "timeline",
    };

    expect(normalizeInboundServerEvent({
      ...base,
      stage: "image_generation",
      phase: "image_generation",
    })).toMatchObject({
      type: "agent.progress",
      stage: "image_generation",
      phase: "image_generation",
    });
    expect(normalizeInboundServerEvent({
      ...base,
      id: "cache:1",
      stage: "cache",
      phase: "cache",
      status: "completed",
      message: "缓存命中",
    })).toMatchObject({
      type: "agent.progress",
      stage: "cache",
      phase: "cache",
    });
    expect(normalizeInboundServerEvent({
      ...base,
      stage: "unsupported",
      phase: "image_generation",
    })).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping server event with invalid semantic payload",
      "agent.progress",
      expect.any(String),
    );
  });

  it("validates provider retry bounds and tool-owned artifact metadata", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const retry = {
      type: "agent.progress",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      id: "provider:openai",
      stage: "status",
      phase: "model",
      status: "running",
      message: "正在重连",
      visibility: "timeline",
      retry_attempt: 2,
      max_retries: 5,
      retry_after_ms: 250,
      provider_state: "reconnecting",
    };
    expect(normalizeInboundServerEvent(retry)).toMatchObject(retry);
    expect(normalizeInboundServerEvent({ ...retry, retry_attempt: 6 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...retry, provider_state: "responding" })).toMatchObject({
      provider_state: "responding",
    });
    expect(normalizeInboundServerEvent({ ...retry, provider_state: "waiting" })).toBeNull();

    const toolResult = {
      type: "tool_result",
      id: "browser-screenshot",
      summary: "截图已生成",
      artifact_id: "artifact-screen-1",
      artifact_kind: "image",
      artifact_media_type: "image/png",
      artifact_bytes: 1_024,
      output_files: [{
        path: "C:\\workspace\\screen.png",
        name: "screen.png",
        size: 1_024,
        mime_type: "image/png",
        is_image: true,
      }],
    };
    expect(normalizeInboundServerEvent(toolResult)).toMatchObject(toolResult);
    // A legacy result may contain only artifact_id; the remaining metadata
    // was added later and must not be required for old transcript replay.
    expect(normalizeInboundServerEvent({
      type: "tool_result",
      id: "legacy-tool",
      summary: "legacy",
      artifact_id: "legacy-artifact",
    })).toMatchObject({ artifact_id: "legacy-artifact" });

    for (const invalid of [
      { ...toolResult, artifact_bytes: -1 },
      { ...toolResult, artifact_bytes: 1.5 },
      { ...toolResult, artifact_bytes: "1024" },
      { ...toolResult, artifact_kind: "" },
      { ...toolResult, artifact_media_type: "image/png;" + "x".repeat(128) },
      { ...toolResult, output_files: [{ path: "screen.png", size: -1 }] },
      { ...toolResult, output_files: [{ path: "screen.png", size: 1, is_image: "yes" }] },
    ]) {
      expect(normalizeInboundServerEvent(invalid)).toBeNull();
    }
    expect(warn).toHaveBeenCalled();
  });

  it("accepts explainable permission decision events", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const event = normalizeInboundServerEvent({
      type: "permission.decision",
      tool_call_id: "tool-1",
      tool_name: "write_file",
      decision: "allow",
      source: "policy",
      permission_level: "auto",
      capability: { allowed: true, reason: "Workspace read allowed" },
      approval_policy: "auto",
      matched_rule: { source: "mode", rule: "auto:auto" },
      risk: "low",
      scope: { workspace_scope: "project", boundary: "filesystem", target: "README.md" },
      expiry: "policy",
    });

    expect(event).toMatchObject({
      type: "permission.decision",
      tool_call_id: "tool-1",
      matched_rule: { source: "mode", rule: "auto:auto" },
      risk: "low",
    });
    expect(warn).not.toHaveBeenCalled();
  });

  it("accepts complete provider-control events and rejects empty or malformed payloads", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent({
      type: "stream_event",
      conversation_id: "conversation-1",
      provider: "openai",
      event_type: "response.output_text.delta",
      data: { delta: "hello" },
      sdk_only: true,
    })).toMatchObject({ type: "stream_event", provider: "openai", sdk_only: true });
    expect(normalizeInboundServerEvent({
      type: "rate_limit",
      conversation_id: "conversation-1",
      provider: "openai",
      error_type: "rate_limit",
      retry_after_seconds: 2.5,
      retry_at: 1234,
      recoverable: true,
    })).toMatchObject({ type: "rate_limit", retry_after_seconds: 2.5 });
    expect(normalizeInboundServerEvent({
      type: "session.state_changed",
      conversation_id: "conversation-1",
      state: "working",
      run_id: "run-1",
      reason: "agent_run_started",
    })).toMatchObject({ type: "session.state_changed", state: "working" });

    expect(normalizeInboundServerEvent({ type: "stream_event" })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "stream_event",
      conversation_id: "conversation-1",
      provider: "openai",
      event_type: "delta",
      data: [],
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "rate_limit",
      conversation_id: "conversation-1",
      error_type: "rate_limit",
      retry_after_seconds: -1,
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "session.state_changed",
      conversation_id: "conversation-1",
      state: "paused",
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "session.state_changed",
      conversation_id: "   ",
      state: "idle",
    })).toBeNull();

    expect(warn).toHaveBeenCalled();
  });

  it("validates complete conversation summary and compaction projections", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent({
      type: "conversation.compaction.updated",
      conversation_id: "conversation-1",
      state: "compacted",
      summary: "Kept the release goal, current findings, and next verification step.",
    })).toMatchObject({
      type: "conversation.compaction.updated",
      conversation_id: "conversation-1",
      state: "compacted",
    });
    expect(normalizeInboundServerEvent({
      type: "conversation.summary.updated",
      conversation_id: "conversation-1",
      summary: "",
      title: "Audit MiniCode release readiness",
      updated_at: "2026-08-15T10:30:00Z",
      memory_mode: "enabled",
      memory_polluted: false,
      memory_pollution_sources: [],
    })).toMatchObject({
      type: "conversation.summary.updated",
      title: "Audit MiniCode release readiness",
    });

    expect(normalizeInboundServerEvent({
      type: "conversation.compaction.updated",
      conversation_id: "conversation-1",
      state: "clean",
      summary: "Old context",
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "conversation.compaction.updated",
      conversation_id: "conversation-1",
      state: "compacted",
      summary: "   ",
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "conversation.summary.updated",
      conversation_id: "conversation-1",
      summary: "Latest result",
      title: "Audit",
      updated_at: "not-a-date",
      memory_mode: "enabled",
      memory_polluted: false,
      memory_pollution_sources: [],
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "conversation.summary.updated",
      conversation_id: "   ",
      summary: "Latest result",
      title: "Audit",
      updated_at: "2026-08-15T10:30:00Z",
      memory_mode: "unknown",
      memory_polluted: true,
      memory_pollution_sources: [""],
    })).toBeNull();

    expect(warn).toHaveBeenCalled();
  });

  it("validates exact context fork, ledger, and side-query payloads", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const fork = {
      type: "context_forked",
      conversation_id: "branch-1",
      fork_id: "fork-1",
      message_index: 4,
      context_history_index: 3,
      history_length: 8,
      estimated_tokens: 2048,
      parent_conversation_id: "conversation-1",
      branch_conversation_id: "branch-1",
      branch_created: true,
      branch_activated: true,
      message_id: "message-5",
      created_at: "2026-08-15T10:30:00Z",
      status: "active",
    };
    const ledger = {
      type: "context_ledger",
      conversation_id: "conversation-1",
      schema_version: 1,
      estimated_tokens: 1800,
      actual_tokens: 1900,
      compaction_count: 2,
      native_attachment_tokens: 300,
      native_attachment_count: 1,
      entries: [{
        category: "files_attachments",
        label: "Native attachments",
        estimated_tokens: 300,
        item_count: 1,
        source_count: 1,
        sources: ["diagram.png"],
      }],
    };
    const sideQuery = {
      type: "context_side_query_result",
      conversation_id: "conversation-1",
      query: "Which verification remains?",
      focus: "release readiness",
      result: "Run the full browser workflow.",
    };

    expect(normalizeInboundServerEvent(fork)).toMatchObject(fork);
    expect(normalizeInboundServerEvent(ledger)).toMatchObject(ledger);
    expect(normalizeInboundServerEvent(sideQuery)).toMatchObject(sideQuery);
    expect(warn).not.toHaveBeenCalled();

    const forkWithoutOwner: Record<string, unknown> = { ...fork };
    delete forkWithoutOwner.conversation_id;
    expect(normalizeInboundServerEvent(forkWithoutOwner)).toBeNull();
    expect(normalizeInboundServerEvent({ ...fork, conversation_id: "conversation-1" })).toBeNull();
    expect(normalizeInboundServerEvent({ ...fork, branch_conversation_id: undefined })).toBeNull();
    expect(normalizeInboundServerEvent({ ...fork, message_index: -1 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...fork, estimated_tokens: 1.5 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...ledger, schema_version: 2 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...ledger, actual_tokens: -1 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...ledger, compaction_count: 0.5 })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...ledger,
      entries: [{ ...ledger.entries[0], category: "unknown" }],
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...ledger,
      entries: [{ ...ledger.entries[0], sources: [""] }],
    })).toBeNull();
    expect(normalizeInboundServerEvent({ ...sideQuery, query: "   " })).toBeNull();
    expect(normalizeInboundServerEvent({ ...sideQuery, result: 7 })).toBeNull();
    expect(normalizeInboundServerEvent({ ...sideQuery, focus: [] })).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("validates all control-request subtypes and rejects unowned blocking prompts", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const canUseTool = {
      type: "control_request",
      request_id: "control-tool-1",
      conversation_id: "conversation-1",
      turn_id: "turn-1",
      timeout_seconds: 300,
      expires_at: 1786789800000,
      request: {
        subtype: "can_use_tool",
        tool_name: "write_file",
        input: { path: "src/app.ts" },
        tool_use_id: "control-tool-1",
        diff: { files: [{ path: "src/app.ts", patch: "+new" }] },
        source_agent: "reviewer",
        source_thread: "conversation-1",
        source_tool: "write_file",
      },
    };
    const elicitation = {
      type: "control_request",
      request_id: "control-ask-1",
      conversation_id: "conversation-1",
      request: {
        subtype: "elicitation",
        tool_use_id: "control-ask-1",
        prompt: "Choose the runtime used for verification.",
        question: "Which runtime should be used?",
        schema: { type: "string", enum: ["node", "bun"] },
        choices: [
          { label: "Node.js", value: "node" },
          { label: "Bun", value: "bun" },
        ],
      },
    };
    const providerPrompt = {
      type: "control_request",
      request_id: "control-auth-1",
      conversation_id: "conversation-1",
      request: {
        subtype: "provider_auth_prompt",
        provider: "github-copilot",
        prompt: "Enter the device verification code.",
        prompt_type: "manual_code",
        placeholder: "ABCD-1234",
        allow_empty: false,
        allow_custom: true,
      },
    };

    expect(normalizeInboundServerEvent(canUseTool)).toMatchObject(canUseTool);
    expect(normalizeInboundServerEvent(elicitation)).toMatchObject(elicitation);
    expect(normalizeInboundServerEvent(providerPrompt)).toMatchObject(providerPrompt);
    expect(warn).not.toHaveBeenCalled();

    const controlWithoutOwner: Record<string, unknown> = { ...canUseTool };
    delete controlWithoutOwner.conversation_id;
    expect(normalizeInboundServerEvent(controlWithoutOwner)).toBeNull();
    expect(normalizeInboundServerEvent({ ...canUseTool, request_id: " " })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...canUseTool,
      request: { ...canUseTool.request, subtype: "future_prompt" },
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...canUseTool,
      request: { ...canUseTool.request, tool_use_id: "another-request" },
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...canUseTool,
      request: { ...canUseTool.request, input: [] },
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...elicitation,
      request: { ...elicitation.request, tool_use_id: "another-request" },
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...elicitation,
      request: { ...elicitation.request, schema: [] },
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...elicitation,
      request: { ...elicitation.request, choices: {} },
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      ...providerPrompt,
      request: { ...providerPrompt.request, provider: "" },
    })).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "approval.file_diff",
      tool_call_id: "unowned-diff",
      path: "src/app.ts",
      patch: "+unsafe",
      is_large: false,
      is_truncated: false,
    })).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("accepts precise owner-scoped provider OAuth events and rejects unsafe projections", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const validEvents = [
      {
        type: "llm.provider.oauth.auth",
        conversation_id: "conversation-1",
        provider: "github-copilot",
        url: "https://github.com/login/oauth/authorize?state=expected",
        instructions: "Complete authorization in the browser.",
      },
      {
        type: "llm.provider.oauth.device_code",
        conversation_id: "conversation-1",
        provider: "openai",
        userCode: "ABCD-EFGH",
        verificationUri: "https://auth.openai.com/device",
        intervalSeconds: 5,
        expiresInSeconds: 900,
      },
      {
        type: "llm.provider.oauth.info",
        conversation_id: "conversation-1",
        provider: "anthropic",
        message: "Complete login in the browser.",
        links: [{ url: "https://claude.ai/oauth/authorize", label: "Authorization page" }],
      },
      {
        type: "llm.provider.oauth.progress",
        conversation_id: "conversation-1",
        provider: "anthropic",
        message: "Exchanging the authorization code.",
      },
    ];

    for (const event of validEvents) {
      expect(normalizeInboundServerEvent(event)).toMatchObject(event);
    }
    expect(warn).not.toHaveBeenCalled();

    const invalidEvents = [
      { ...validEvents[0], conversation_id: "" },
      { ...validEvents[0], provider: "" },
      { ...validEvents[0], url: "javascript:alert(1)" },
      { ...validEvents[0], url: "https://user:password@example.test/authorize" },
      { ...validEvents[1], intervalSeconds: 0 },
      { ...validEvents[1], expiresInSeconds: Number.POSITIVE_INFINITY },
      { ...validEvents[2], links: [{ url: "file:///tmp/oauth", label: "Unsafe" }] },
      { ...validEvents[2], links: [{ url: "https://example.test", label: "" }] },
      { ...validEvents[3], message: "" },
    ];
    for (const event of invalidEvents) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("validates every provider OAuth prompt shape and select option identity", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const base = {
      type: "control_request",
      request_id: "provider-auth",
      conversation_id: "conversation-1",
    };
    const textPrompt = {
      ...base,
      request: {
        subtype: "provider_auth_prompt",
        provider: "provider-one",
        prompt: "Enter a value",
        prompt_type: "text",
        placeholder: "value",
        allow_empty: true,
        allow_custom: true,
      },
    };
    const secretPrompt = {
      ...base,
      request_id: "provider-secret",
      request: {
        ...textPrompt.request,
        prompt_type: "secret",
        allow_empty: false,
      },
    };
    const manualPrompt = {
      ...base,
      request_id: "provider-manual",
      request: {
        ...textPrompt.request,
        prompt_type: "manual_code",
        allow_empty: false,
      },
    };
    const selectPrompt = {
      ...base,
      request_id: "provider-select",
      request: {
        subtype: "provider_auth_prompt",
        provider: "openai",
        prompt: "Choose a login method",
        prompt_type: "select",
        allow_empty: false,
        allow_custom: false,
        options: [
          { id: "browser", label: "Browser", description: "Use a callback page" },
          { id: "device_code", label: "Device code" },
        ],
      },
    };

    for (const event of [textPrompt, secretPrompt, manualPrompt, selectPrompt]) {
      expect(normalizeInboundServerEvent(event)).toMatchObject(event);
    }

    const invalidRequests = [
      { ...selectPrompt.request, allow_custom: true },
      { ...selectPrompt.request, allow_empty: true },
      { ...selectPrompt.request, options: [] },
      {
        ...selectPrompt.request,
        options: [
          { id: "browser", label: "Browser" },
          { id: "browser", label: "Duplicate" },
        ],
      },
      { ...textPrompt.request, options: [{ id: "unexpected", label: "Unexpected" }] },
      { ...textPrompt.request, allow_custom: false },
      { ...textPrompt.request, prompt_type: "future_prompt" },
    ];
    for (const request of invalidRequests) {
      expect(normalizeInboundServerEvent({ ...base, request })).toBeNull();
    }
  });

  it("accepts mcp lifecycle and progress events from the backend contract", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const lifecycle = normalizeInboundServerEvent({
      type: "mcp.lifecycle",
      server_name: "demo",
      phase: "auth_required",
      requires_user_action: true,
    });
    expect(lifecycle).toMatchObject({ type: "mcp.lifecycle", server_name: "demo" });

    const progress = normalizeInboundServerEvent({
      type: "mcp.progress",
      server_name: "demo",
      operation: "connect",
      status: "running",
    });
    expect(progress).toMatchObject({ type: "mcp.progress", server_name: "demo" });

    expect(warn).not.toHaveBeenCalled();
  });

  it("validates owned agent deltas and every user-message queue transition", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent({
      type: "agent_message.delta",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      item_id: "answer-1",
      delta: " ",
    })).toMatchObject({ type: "agent_message.delta", delta: " " });
    expect(normalizeInboundServerEvent({
      type: "user_message.queue.updated",
      status: "queued",
      conversation_id: "conversation-1",
      message_id: "assistant-2",
      user_message_id: "user-2",
      position: 1,
    })).toMatchObject({ status: "queued", position: 1 });
    expect(normalizeInboundServerEvent({
      type: "user_message.queue.updated",
      status: "dequeued",
      conversation_id: "conversation-1",
      message_id: "assistant-2",
      turn_mode: "follow_up",
    })).toMatchObject({ status: "dequeued", turn_mode: "follow_up" });
    expect(normalizeInboundServerEvent({
      type: "user_message.queue.updated",
      status: "dequeued",
      conversation_id: "conversation-1",
      message_id: "assistant-steer",
      reason: "steered_current_turn",
      turn_mode: "steer",
    })).toMatchObject({ status: "dequeued", turn_mode: "steer" });

    const invalid = [
      { type: "agent_message.delta", item_id: "answer-1", delta: "missing owner" },
      { type: "agent_message.delta", conversation_id: "conversation-1", item_id: "", delta: "text" },
      { type: "agent_message.delta", conversation_id: "conversation-1", item_id: "answer-1", delta: "" },
      {
        type: "user_message.queue.updated",
        status: "queued",
        conversation_id: "conversation-1",
        message_id: "assistant-2",
        position: 0,
      },
      {
        type: "user_message.queue.updated",
        status: "waiting",
        conversation_id: "conversation-1",
        message_id: "assistant-2",
      },
      {
        type: "user_message.queue.updated",
        status: "dequeued",
        conversation_id: "conversation-1",
        message_id: "assistant-2",
        position: 1,
      },
      {
        type: "user_message.queue.updated",
        status: "dequeued",
        conversation_id: "conversation-1",
        message_id: "assistant-steer",
        turn_mode: "steer",
      },
      {
        type: "user_message.queue.updated",
        status: "cancelled",
        conversation_id: "conversation-1",
        message_id: "assistant-2",
        turn_mode: "follow_up",
      },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
    expect(warn).toHaveBeenCalled();
  });

  it("bounds control approval payloads while preserving actionable policy evidence", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const approval = {
      type: "control_request",
      request_id: "call-1",
      conversation_id: "conversation-1",
      workspace_root: "C:\workspace",
      permission_mode: "confirm",
      workspace_scope: "worktree",
      timeout_seconds: 300,
      expires_at: 1_786_789_800_000,
      request: {
        subtype: "can_use_tool",
        tool_use_id: "call-1",
        tool_name: "write_file",
        input: { path: "src/app.ts", content: "replacement" },
        source_agent: "reviewer",
        source_thread: "conversation-1",
        source_tool: "write_file",
        diff: { format: "structured", files: [{ path: "src/app.ts", patch: "+replacement" }] },
      },
    };
    expect(normalizeInboundServerEvent(approval)).toMatchObject({
      type: "control_request",
      request_id: "call-1",
      permission_mode: "confirm",
    });

    let deep: Record<string, unknown> = {};
    let cursor = deep;
    for (let index = 0; index < 14; index += 1) {
      const next: Record<string, unknown> = {};
      cursor.next = next;
      cursor = next;
    }
    const invalid = [
      { ...approval, request: { ...approval.request, source_agent: "" } },
      { ...approval, timeout_seconds: 0 },
      { ...approval, expires_at: Number.POSITIVE_INFINITY },
      { ...approval, request: { ...approval.request, input: deep } },
      { ...approval, request: { ...approval.request, input: { content: "x".repeat(262_145) } } },
      { ...approval, request: { ...approval.request, diff: ["unexpected"] } },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
    expect(warn).toHaveBeenCalled();
  });

  it("validates checkpoint timestamps, relative paths, metadata bounds, and collection owners", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { type: _type, ...record } = checkpointEvent();
    expect(normalizeInboundServerEvent(checkpointEvent())).toMatchObject({ id: "cp-1" });
    expect(normalizeInboundServerEvent({
      type: "checkpoint.list",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
      checkpoints: [record],
    })).toMatchObject({ type: "checkpoint.list" });

    let deepMetadata: Record<string, unknown> = {};
    let metadataCursor = deepMetadata;
    for (let index = 0; index < 14; index += 1) {
      const next: Record<string, unknown> = {};
      metadataCursor.next = next;
      metadataCursor = next;
    }
    const invalid = [
      checkpointEvent({ created_at: "not-a-date" }),
      checkpointEvent({ paths: [] }),
      checkpointEvent({ paths: ["C:\\workspace\\src\\app.ts"] }),
      checkpointEvent({ paths: ["src/../secret.ts"] }),
      checkpointEvent({ metadata: deepMetadata }),
      {
        type: "checkpoint.list",
        conversation_id: "conversation-1",
        workspace_root: "C:\\workspace",
        checkpoints: [{ ...record, conversation_id: "conversation-2" }],
      },
      {
        type: "checkpoint.rewound",
        conversation_id: "conversation-1",
        workspace_root: "C:\\workspace",
        checkpoint: { ...record, workspace_root: "D:\\other" },
      },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
    expect(warn).toHaveBeenCalled();
  });

  it("validates preview refresh evidence and both terminal-output wire shapes", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    expect(normalizeInboundServerEvent({
      type: "preview.refreshed",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
      request_id: "refresh-1",
      path: "src/app.ts",
      url: "http://localhost:5173/app",
    })).toMatchObject({ type: "preview.refreshed", path: "src/app.ts" });
    expect(normalizeInboundServerEvent({
      type: "preview.refreshed",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
    })).toMatchObject({ type: "preview.refreshed" });
    expect(normalizeInboundServerEvent({
      type: "terminal.output",
      conversation_id: "conversation-1",
      session_id: "terminal-1",
      data: "build output",
    })).toMatchObject({ session_id: "terminal-1", data: "build output" });
    expect(normalizeInboundServerEvent({
      type: "terminal.output",
      conversation_id: "conversation-1",
      command: "npm test",
      output: "passed",
      exit_code: 0,
    })).toMatchObject({ command: "npm test", exit_code: 0 });
    expect(normalizeInboundServerEvent({
      type: "terminal.output",
      conversation_id: "conversation-1",
      command: "still running",
      output: "",
    })).toMatchObject({ command: "still running" });

    const invalid = [
      {
        type: "preview.refreshed",
        conversation_id: "conversation-1",
        workspace_root: "C:\\workspace",
        url: "http://user:password@localhost:5173",
      },
      {
        type: "preview.refreshed",
        conversation_id: "conversation-1",
        workspace_root: "C:\\workspace",
        path: "C:\\workspace\\src\\app.ts",
      },
      {
        type: "terminal.output",
        conversation_id: "conversation-1",
        command: "npm test",
        output: "pending",
        exit_code: null,
      },
      {
        type: "terminal.output",
        conversation_id: "conversation-1",
        session_id: "terminal-1",
        data: "chunk",
        command: "npm test",
        output: "mixed",
      },
      {
        type: "terminal.output",
        conversation_id: "conversation-1",
        session_id: "terminal-1",
        data: "",
      },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
    expect(warn).toHaveBeenCalled();
  });

  it("validates runtime spans with exact tool correlation and timing evidence", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const span = {
      type: "runtime.span",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      event: "tool.completed",
      span_id: "span-1",
      run_id: "run-1",
      turn_id: "turn-1",
      phase: "tool",
      status: "completed",
      started_at: 100,
      ended_at: 145,
      duration_ms: 45,
      tool_call_id: "call-1",
      tool_name: "read_file",
      ui_visible: true,
      debug_only: false,
      data: { exit_code: 0 },
    };
    expect(normalizeInboundServerEvent(span)).toMatchObject(span);

    const invalid = [
      { ...span, ui_visible: undefined },
      { ...span, status: "unknown" },
      { ...span, event: "provider.completed" },
      { ...span, phase: "provider" },
      { ...span, tool_name: undefined },
      { ...span, tool_call_id: undefined },
      { ...span, ended_at: 99 },
      { ...span, duration_ms: 44 },
      { ...span, data: { nested: { value: Number.NaN } } },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("validates terminal done usage, recoverability, and bounded provider diagnostics", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const done = {
      type: "done",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      status: "failed",
      reason: "provider disconnected",
      duration_ms: 250,
      failure_recoverable: true,
      usage: {
        input_tokens: 100,
        output_tokens: 25,
        cache_creation_input_tokens: 10,
        cache_read_input_tokens: 40,
        input_includes_cache_read: true,
        cache_deleted_input_tokens: 5,
        prompt_cache_total_tokens: 150,
        prompt_cache_hit_rate: 26.7,
        reasoning_output_tokens: 8,
        cost_usd: 0.0125,
      },
      provider_raw: { diagnostics_deferred: true, trace_id: "trace-1" },
    };
    expect(normalizeInboundServerEvent(done)).toMatchObject(done);
    const { failure_recoverable: _failureRecoverable, ...completedDone } = done;
    expect(normalizeInboundServerEvent({
      ...completedDone,
      status: "completed",
    })).toMatchObject({ status: "completed" });

    const invalid = [
      { ...done, status: "running" },
      { ...done, conversation_id: "" },
      { ...done, usage: { ...done.usage, input_tokens: -1 } },
      { ...done, usage: { ...done.usage, output_tokens: Number.NaN } },
      { ...done, usage: { ...done.usage, input_includes_cache_read: "yes" } },
      { ...done, usage: { ...done.usage, prompt_cache_hit_rate: 101 } },
      { ...done, usage: { ...done.usage, cost_usd: Number.POSITIVE_INFINITY } },
      { ...done, status: "completed", failure_recoverable: true },
      { ...done, provider_raw: { payload: "x".repeat(262_145) } },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("accepts bounded global errors without inventing conversation ownership", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const globalError = {
      type: "error",
      message: "Invalid JSON message",
      recoverable: true,
      error_type: "protocol",
      error_code: "invalid_json",
      provider: "websocket",
    };
    expect(normalizeInboundServerEvent(globalError)).toMatchObject(globalError);
    expect(normalizeInboundServerEvent({
      ...globalError,
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      attachments: [{ path: "diagnostic.txt", size: 128 }],
    })).toMatchObject({ conversation_id: "conversation-1" });

    const invalid = [
      { ...globalError, message: "" },
      { ...globalError, recoverable: "yes" },
      { ...globalError, error_type: "" },
      { ...globalError, conversation_id: "x".repeat(1_025) },
      { ...globalError, provider_error_type: "x".repeat(257) },
      { ...globalError, attachments: [Number.NaN] },
      { ...globalError, attachments: [{ payload: "x".repeat(262_145) }] },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("validates context usage, compaction boundaries, and budget projections", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const ledger = {
      schema_version: 1,
      estimated_tokens: 300,
      actual_tokens: 280,
      compaction_count: 2,
      native_attachment_tokens: 20,
      native_attachment_count: 1,
      entries: [{
        category: "history",
        label: "Conversation history",
        estimated_tokens: 200,
        item_count: 4,
        source_count: 2,
        sources: ["turn-1", "turn-2"],
      }],
    };
    const contextUsage = {
      type: "context_usage",
      conversation_id: "conversation-1",
      used: 280,
      limit: 1_000,
      ledger,
    };
    const compacted = {
      type: "context_compacted",
      conversation_id: "conversation-1",
      summary: "Retained current goal and latest tool evidence.",
      before_tokens: 900,
      after_tokens: 280,
      retained_categories: ["history", "system_runtime"],
      ledger,
    };
    const budget = {
      type: "budget_update",
      conversation_id: "conversation-1",
      used: 280,
      total: 1_000,
      breakdown: { history: 200, system_runtime: 80 },
    };
    const warning = {
      type: "budget.warning",
      conversation_id: "conversation-1",
      bucket: "context",
      percent: 0.9,
      will_compact: true,
    };
    expect(normalizeInboundServerEvent(contextUsage)).toMatchObject(contextUsage);
    expect(normalizeInboundServerEvent(compacted)).toMatchObject(compacted);
    expect(normalizeInboundServerEvent(budget)).toMatchObject(budget);
    expect(normalizeInboundServerEvent(warning)).toMatchObject(warning);

    const invalid = [
      { ...contextUsage, used: -1 },
      { ...contextUsage, ledger: { ...ledger, entries: [{ ...ledger.entries[0], sources: ["turn-1", "turn-1"] }] } },
      { ...compacted, before_tokens: Number.NaN },
      { ...compacted, retained_categories: ["history", "history"] },
      { ...budget, breakdown: { history: -1 } },
      { ...budget, breakdown: { history: 1.5 } },
      { ...warning, percent: 1.01 },
      { ...warning, will_compact: "yes" },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("validates conversation and session snapshot ownership, versions, and correlation metadata", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const conversation = {
      id: "conversation-1",
      title: "Conversation 1",
      created_at: "2026-08-15T00:00:00Z",
      updated_at: "2026-08-16T00:00:00Z",
      messages: [],
    };
    const conversationList = {
      type: "conversation.list",
      client_command_id: "cmd_list_latest",
      client_command_type: "conversation.list",
      timestamp: "2026-08-16T00:00:01Z",
      snapshot_at: "2026-08-16T00:00:00Z",
      active_conversation_id: "conversation-1",
      conversations: [conversation],
      active_conversation: conversation,
      session: {
        session_id: "session-1",
        active_conversation_id: "conversation-1",
        active_conversation: conversation,
        provider_capabilities: {
          reasoning_effort_levels: ["low", "medium", "high"],
        },
      },
    };

    expect(normalizeInboundServerEvent(conversationList)).toMatchObject({
      type: "conversation.list",
      client_command_id: "cmd_list_latest",
      client_command_type: "conversation.list",
      active_conversation_id: "conversation-1",
    });
    expect(normalizeInboundServerEvent({
      type: "session.restored",
      client_command_id: "cmd_restore_latest",
      client_command_type: "session.restore",
      timestamp: "2026-08-16T00:00:02Z",
      snapshot_at: "2026-08-16T00:00:01Z",
      active_conversation_id: "conversation-1",
      conversation,
      active_conversation: conversation,
      last_seq: 10,
      current_seq: 12,
      replayed_events: 2,
      session: {
        session_id: "session-1",
        active_conversation_id: "conversation-1",
        active_conversation: conversation,
      },
    })).toMatchObject({ type: "session.restored", current_seq: 12 });
    expect(normalizeInboundServerEvent({
      type: "session.restored",
      last_seq: 8,
      current_seq: 8,
      requested_last_seq: 12,
      replayed_events: 0,
      missed_events: true,
      event_log_gap: true,
      snapshot_required: true,
      cursor_reset: true,
    })).toMatchObject({ type: "session.restored", cursor_reset: true, current_seq: 8 });

    const invalid = [
      { ...conversationList, conversations: [conversation, { ...conversation }] },
      { ...conversationList, active_conversation_id: "conversation-missing" },
      { ...conversationList, active_conversation: { ...conversation, id: "conversation-other" } },
      { ...conversationList, snapshot_at: "not-a-date" },
      { ...conversationList, client_command_id: 42 },
      { ...conversationList, client_command_type: "x".repeat(257) },
      { ...conversationList, timestamp: "not-a-date" },
      { ...conversationList, event_id: "" },
      {
        type: "session.restored",
        active_conversation_id: "conversation-1",
        conversation: { ...conversation, id: "conversation-other" },
      },
      {
        type: "session.synced",
        active_conversation_id: "conversation-1",
        active_conversation: { ...conversation, id: "conversation-other" },
      },
      { type: "session.restored", last_seq: 12, current_seq: 11 },
      {
        type: "session.restored",
        last_seq: 8,
        current_seq: 8,
        requested_last_seq: 12,
        replayed_events: 1,
        snapshot_required: true,
        cursor_reset: true,
      },
      {
        type: "session.restored",
        last_seq: 8,
        current_seq: 8,
        requested_last_seq: 7,
        replayed_events: 0,
        snapshot_required: true,
        cursor_reset: true,
      },
      {
        type: "session.restored",
        last_seq: 8,
        current_seq: 8,
        requested_last_seq: 8,
        event_log_gap: true,
        snapshot_required: false,
      },
      {
        type: "session.restored",
        session: {
          capabilities: {
            provider_capabilities: {
              reasoning_effort_levels: () => ["high"],
            },
          },
        },
      },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
    expect(warn).toHaveBeenCalled();
  });

  it("requires an atomic non-empty inventory epoch tuple", () => {
    const valid = {
      type: "conversation.list",
      inventory_instance_id: "0123456789abcdef0123456789abcdef",
      inventory_revision: 7,
      active_conversation_id: null,
      active_conversation: null,
      conversations: [],
    };
    expect(normalizeInboundServerEvent(valid)).toMatchObject(valid);

    for (const event of [
      { ...valid, inventory_instance_id: "" },
      { ...valid, inventory_instance_id: "   " },
      { ...valid, inventory_revision: -1 },
      { ...valid, inventory_revision: 1.5 },
      { ...valid, inventory_revision: true },
      { ...valid, inventory_revision: Number.MAX_SAFE_INTEGER + 1 },
      { ...valid, inventory_instance_id: undefined },
      { ...valid, inventory_revision: undefined },
    ]) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("accepts safe goal revisions and rejects stale-wire numeric coercions", () => {
    const valid = {
      type: "goal.updated",
      conversation_id: "conversation-1",
      goal: {
        id: "goal-1",
        text: "Ship verified behavior",
        status: "active",
        updated_at: "2026-08-16T00:00:00Z",
      },
      source: "test",
      updated_at: "2026-08-16T00:00:00Z",
      revision: 9,
    };
    expect(normalizeInboundServerEvent(valid)).toMatchObject(valid);

    for (const revision of [-1, 1.5, true, Number.MAX_SAFE_INTEGER + 1]) {
      expect(normalizeInboundServerEvent({ ...valid, revision })).toBeNull();
    }
  });

  it("requires owned, non-ambiguous approval cancellation payloads", () => {
    const valid = {
      type: "approval.cancelled",
      conversation_id: "conversation-1",
      request_ids: ["approval-1", "approval-2"],
      reason: "turn_finished",
    };
    expect(normalizeInboundServerEvent(valid)).toMatchObject(valid);

    for (const event of [
      { ...valid, conversation_id: undefined },
      { ...valid, request_ids: [] },
      { ...valid, request_ids: ["approval-1", "approval-1"] },
      { ...valid, request_ids: [""] },
      { ...valid, reason: "x".repeat(257) },
    ]) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("validates complete stream resume snapshots including long answers and tool identity", () => {
    const longAnswer = "x".repeat(4_194_304);
    const valid = {
      type: "stream_resume",
      conversation_id: "conversation-1",
      message_id: "assistant-1",
      turn_id: "turn-1",
      event_seq: 12,
      stream_status: "running",
      content_blocks: [{
        type: "text",
        itemId: "agent-message",
        content: longAnswer,
        status: "partial",
        isStreaming: false,
      }],
      tool_calls_pending: [{
        id: "tool-1",
        name: "read_file",
        args: { path: "src/app.ts" },
        status: "running",
      }],
      tool_states: [{
        id: "tool-1",
        name: "read_file",
        status: "running",
      }],
    };
    expect(normalizeInboundServerEvent(valid)).toMatchObject({
      type: "stream_resume",
      event_seq: 12,
      content_blocks: [expect.objectContaining({ content: longAnswer })],
    });

    const invalid = [
      { ...valid, message_id: undefined },
      { ...valid, event_seq: -1 },
      { ...valid, tool_calls_pending: [{ id: "tool-1", args: {} }] },
      { ...valid, tool_calls_pending: [{ id: "tool-1", name: "read_file" }] },
      { ...valid, tool_calls_pending: [{ id: "tool-1", name: "read_file", args: [] }] },
      {
        ...valid,
        tool_calls_pending: [
          { id: "tool-1", name: "read_file", args: {} },
          { id: "tool-1", name: "write_file", args: {} },
        ],
      },
      {
        ...valid,
        content_blocks: [{ type: "text", content: `${longAnswer}x` }],
      },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("rejects nested, unordered, miscounted, and out-of-window replay envelopes", () => {
    const valid = {
      type: "session.replay",
      last_seq: 4,
      current_seq: 7,
      replayed_events: 2,
      events: [
        {
          type: "agent.item",
          conversation_id: "conversation-1",
          message_id: "assistant-1",
          id: "process-5",
          kind: "status",
          content: "working",
          status: "completed",
          seq: 5,
          previous_replay_seq: 4,
        },
        {
          type: "done",
          conversation_id: "conversation-1",
          message_id: "assistant-1",
          status: "completed",
          usage: {},
          seq: 7,
          previous_replay_seq: 5,
        },
      ],
    };
    expect(normalizeInboundServerEvent(valid)).toMatchObject({
      type: "session.replay",
      replayed_events: 2,
    });

    const invalid = [
      { ...valid, replayed_events: 1 },
      { ...valid, current_seq: 3 },
      { ...valid, events: [valid.events[1], valid.events[0]] },
      { ...valid, events: [{ type: "session.replay", events: [] }] },
      { ...valid, events: [{ type: "conversation.switched", conversation_id: "conversation-1" }] },
      { ...valid, events: [{ ...valid.events[0], seq: 4 }] },
      { ...valid, events: [{ ...valid.events[0], seq: 8 }] },
      { ...valid, events: [{ ...valid.events[0], previous_replay_seq: 3 }, valid.events[1]] },
      { ...valid, events: [valid.events[0], { ...valid.events[1], previous_replay_seq: 6 }] },
      { ...valid, events: [{ ...valid.events[0], previous_replay_seq: undefined }, valid.events[1]] },
      { ...valid, current_seq: 8 },
      { ...valid, last_seq: undefined },
    ];
    for (const event of invalid) {
      expect(normalizeInboundServerEvent(event)).toBeNull();
    }
  });

  it("drops unknown server events", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const event = normalizeInboundServerEvent({ type: "future.event", payload: true });

    expect(event).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping unknown server event type",
      "future.event",
    );
  });

  it("drops malformed payloads that cannot be routed", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent(null)).toBeNull();
    expect(normalizeInboundServerEvent({ seq: 1 })).toBeNull();

    expect(warn).toHaveBeenCalledWith("[ws] Dropping non-object server event", null);
    expect(warn).toHaveBeenCalledWith("[ws] Dropping server event without a string type", { seq: 1 });
  });

  it("drops malformed envelope fields", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const event = normalizeInboundServerEvent({ type: "pong", seq: "1", event_id: 1 });

    expect(event).toBeNull();
    expect(warn).toHaveBeenCalledWith("[ws] Dropping server event with invalid seq", "pong", "1");
  });

  it("drops events missing routing fields required by their contract", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent({ type: "tool_call", name: "read_file", args: {} })).toBeNull();
    expect(normalizeInboundServerEvent({ type: "session.replay" })).toBeNull();

    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping server event with invalid routing field",
      "tool_call",
      "id",
    );
  });

  it("validates the semantic payloads of control-plane projection events", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent({
      type: "conversation.hydration.updated",
      conversation_id: "conversation-1",
      is_hydrating: false,
    })).toMatchObject({ type: "conversation.hydration.updated", is_hydrating: false });
    expect(normalizeInboundServerEvent({
      type: "permission.rules.updated",
      session_id: "session-1",
      conversation_id: "conversation-1",
      source: "websocket.command",
      rules: {
        mode: "confirm",
        context_source: "conversation.runtime",
        system_deny: [{ pattern: "run_command(rm:*)", source: "system.always_deny" }],
        session_deny: [],
        session_overrides: [{ pattern: "read_file(*)", level: "allow", source: "conversation.runtime" }],
        session_prompt_rules: [],
      },
    })).toMatchObject({ type: "permission.rules.updated" });
    expect(normalizeInboundServerEvent({
      type: "workspace.recent.list",
      projects: [{ path: "C:\\repo", name: "repo", project_type: "python", last_opened: 10 }],
    })).toMatchObject({ type: "workspace.recent.list" });
    expect(normalizeInboundServerEvent({
      type: "guidelines.updated",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
      message: "Project guidelines have been updated",
    })).toMatchObject({ type: "guidelines.updated" });

    expect(normalizeInboundServerEvent({
      type: "permission.rules.updated",
      session_id: "session-1",
      conversation_id: "conversation-1",
      source: "websocket.command",
      rules: {
        mode: "confirm",
        context_source: "conversation.runtime",
        system_deny: [{ pattern: 42, source: "system.always_deny" }],
        session_deny: [],
        session_overrides: [],
        session_prompt_rules: [],
      },
    })).toBeNull();
    expect(normalizeInboundServerEvent(checkpointEvent({ paths: ["src/app.ts", 7] }))).toBeNull();
    expect(normalizeInboundServerEvent({
      type: "checkpoint.run.resume",
      conversation_id: "conversation-1",
      workspace_root: "C:\\workspace",
      resumed: true,
      session_id: "session-1",
      run_id: "run-1",
      iteration: "3",
    })).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping server event with invalid semantic payload",
      "permission.rules.updated",
      expect.any(String),
    );
  });

  it("accepts a fully owned background stall and rejects a stall without prompt evidence", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent({
      type: "background.stalled",
      command_id: "bg-1",
      command: "npm create vite",
      conversation_id: "conversation-1",
      tail: "Overwrite existing files? [y/N]",
      advice: "Re-run with piped input or a non-interactive flag.",
    })).toMatchObject({
      type: "background.stalled",
      command_id: "bg-1",
      conversation_id: "conversation-1",
    });

    expect(normalizeInboundServerEvent({
      type: "background.stalled",
      command_id: "bg-1",
      conversation_id: "conversation-1",
      advice: "Re-run non-interactively.",
    })).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping server event with invalid routing field",
      "background.stalled",
      "tail",
    );
  });

  it("drops owned events when either owner boundary is missing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const withoutConversation = checkpointEvent();
    delete withoutConversation.conversation_id;
    const withoutWorkspace = checkpointEvent();
    delete withoutWorkspace.workspace_root;
    expect(normalizeInboundServerEvent(withoutConversation)).toBeNull();
    expect(normalizeInboundServerEvent(withoutWorkspace)).toBeNull();

    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping server event without conversation owner",
      "checkpoint.created",
    );
    expect(warn).toHaveBeenCalledWith(
      "[ws] Dropping server event without workspace owner",
      "checkpoint.created",
    );
  });

  it("accepts artifact content owned by a projectless conversation", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    expect(normalizeInboundServerEvent({
      type: "artifact_content",
      artifact_id: "artifact-image-1",
      conversation_id: "conversation-projectless",
      workspace_root: "",
      request_id: "preview-request-1",
      content: "AA==",
      media_type: "image/png",
      url: "data:image/png;base64,AA==",
    })).toMatchObject({
      type: "artifact_content",
      artifact_id: "artifact-image-1",
      conversation_id: "conversation-projectless",
      workspace_root: "",
    });
    expect(warn).not.toHaveBeenCalled();
  });
});
