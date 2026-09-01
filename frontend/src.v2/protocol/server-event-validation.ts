import { SERVER_EVENT_TYPES, type ServerEvent, type ServerEventType } from "./events";
import {
  AGENT_PROGRESS_PHASES,
  AGENT_PROGRESS_PROVIDER_STATES,
  AGENT_PROGRESS_STAGES,
  AGENT_PROGRESS_STATUSES,
} from "./streaming-types";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

type FieldKind = "array" | "boolean" | "number" | "record" | "string";

const REQUIRED_ROUTING_FIELDS: Partial<
  Record<ServerEventType, Readonly<Record<string, FieldKind>>>
> = {
  "item.started": { conversation_id: "string", message_id: "string", item: "record" },
  "agent_message.delta": { item_id: "string", delta: "string" },
  "item.completed": { conversation_id: "string", message_id: "string", item: "record" },
  "thinking_delta": { conversation_id: "string", message_id: "string", content: "string" },
  "thinking": { conversation_id: "string", message_id: "string", content: "string" },
  "agent.item": { conversation_id: "string", message_id: "string", id: "string", kind: "string" },
  "agent.progress": {
    conversation_id: "string",
    message_id: "string",
    id: "string",
    stage: "string",
    status: "string",
    message: "string",
  },
  "runtime.span": {
    conversation_id: "string",
    message_id: "string",
    event: "string",
    span_id: "string",
    status: "string",
  },
  "agent.run.started": { run_id: "string", status: "string" },
  "agent.run.completed": { run_id: "string", status: "string" },
  "approval.file_diff": {
    tool_call_id: "string",
    conversation_id: "string",
    path: "string",
    patch: "string",
    is_large: "boolean",
    is_truncated: "boolean",
  },
  "approval.cancelled": { conversation_id: "string", request_ids: "array" },
  "permission.decision": { tool_call_id: "string", tool_name: "string", decision: "string" },
  "tool_call": { id: "string", name: "string", args: "record" },
  "tool_output_delta": { id: "string", output: "string" },
  "command_output_chunk": {
    conversation_id: "string",
    message_id: "string",
    content: "string",
    stream: "string",
  },
  "tool_result": { id: "string", summary: "string" },
  "stream_event": { provider: "string", event_type: "string", data: "record" },
  "rate_limit": { error_type: "string" },
  "session.state_changed": { state: "string" },
  "context_forked": {
    conversation_id: "string",
    fork_id: "string",
    message_index: "number",
    context_history_index: "number",
    history_length: "number",
    estimated_tokens: "number",
    parent_conversation_id: "string",
    branch_created: "boolean",
    branch_activated: "boolean",
  },
  "context_ledger": {
    conversation_id: "string",
    schema_version: "number",
    estimated_tokens: "number",
    actual_tokens: "number",
    compaction_count: "number",
    native_attachment_tokens: "number",
    native_attachment_count: "number",
    entries: "array",
  },
  "context_side_query_result": {
    conversation_id: "string",
    query: "string",
    result: "string",
    focus: "string",
  },
  "client.command.ack": { client_command_id: "string", command_type: "string" },
  "commands.list": { commands: "array" },
  "image_chunk": {
    conversation_id: "string",
    message_id: "string",
    media_type: "string",
  },
  "parent.notifications": {
    conversation_id: "string",
    parent_run_id: "string",
    count: "number",
  },
  "system_notice": { conversation_id: "string" },
  "workspace.imported": {
    conversation_id: "string",
    workspace_root: "string",
    project: "record",
    summary: "string",
    file_count: "number",
  },
  "user_message.queue.updated": { status: "string", message_id: "string" },
  "control_request": {
    request_id: "string",
    conversation_id: "string",
    request: "record",
  },
  "llm.provider.oauth.auth": {
    conversation_id: "string",
    provider: "string",
    url: "string",
  },
  "llm.provider.oauth.device_code": {
    conversation_id: "string",
    provider: "string",
    userCode: "string",
    verificationUri: "string",
  },
  "llm.provider.oauth.info": {
    conversation_id: "string",
    provider: "string",
    message: "string",
  },
  "llm.provider.oauth.progress": {
    conversation_id: "string",
    provider: "string",
    message: "string",
  },
  "artifact_content": {
    artifact_id: "string",
    conversation_id: "string",
    workspace_root: "string",
    request_id: "string",
  },
  "session.replay": { events: "array" },
  "stream_resume": {
    conversation_id: "string",
    tool_calls_pending: "array",
  },
  "goal.updated": { conversation_id: "string", goal: "record" },
  "context_usage": { used: "number", limit: "number" },
  "context_compacted": { summary: "string" },
  "budget_update": { used: "number", total: "number", breakdown: "record" },
  "budget.warning": { bucket: "string", percent: "number", will_compact: "boolean" },
  "done": { conversation_id: "string", message_id: "string", status: "string", usage: "record" },
  "error": { message: "string", recoverable: "boolean", error_type: "string" },
  "subagent.start": { subagent_id: "string" },
  "subagent.progress": { subagent_id: "string" },
  "subagent.done": { subagent_id: "string" },
  "subagent.plan_approval_requested": {
    subagent_id: "string",
    request_id: "string",
  },
  "turn.plan.updated": {
    thread_id: "string",
    conversation_id: "string",
    turn_id: "string",
    plan: "array",
  },
  "turn.diff.updated": {
    thread_id: "string",
    conversation_id: "string",
    turn_id: "string",
    diff: "string",
  },
  "conversation.hydration.updated": {
    conversation_id: "string",
    is_hydrating: "boolean",
  },
  "conversation.compaction.updated": {
    conversation_id: "string",
    state: "string",
    summary: "string",
  },
  "conversation.summary.updated": {
    conversation_id: "string",
    summary: "string",
    title: "string",
    updated_at: "string",
    memory_mode: "string",
    memory_polluted: "boolean",
    memory_pollution_sources: "array",
  },
  "permission.rules.updated": {
    session_id: "string",
    conversation_id: "string",
    source: "string",
    rules: "record",
  },
  "workspace.recent.list": { projects: "array" },
  "checkpoint.created": {
    id: "string",
    session_id: "string",
    tool_call_id: "string",
    tool_name: "string",
    paths: "array",
    created_at: "string",
    metadata: "record",
  },
  "checkpoint.list": { checkpoints: "array" },
  "checkpoint.rewound": { checkpoint: "record" },
  "checkpoint.run.list": {
    session_id: "string",
    checkpoints: "array",
    runs: "array",
    subagents: "array",
  },
  "checkpoint.run.resume": { resumed: "boolean" },
  "guidelines.updated": {
    message: "string",
    conversation_id: "string",
    workspace_root: "string",
  },
  "background.started": {
    command_id: "string",
    conversation_id: "string",
    status: "string",
  },
  "background.stalled": {
    command_id: "string",
    conversation_id: "string",
    tail: "string",
    advice: "string",
  },
  "background.completed": {
    command_id: "string",
    conversation_id: "string",
    status: "string",
  },
};

const CONVERSATION_OWNED_EVENT_TYPES = new Set<ServerEventType>([
  "item.started",
  "agent_message.delta",
  "item.completed",
  "thinking_delta",
  "thinking",
  "agent.item",
  "agent.progress",
  "runtime.span",
  "agent.run.started",
  "agent.run.completed",
  "done",
  "context_usage",
  "context_compacted",
  "budget_update",
  "budget.warning",
  "approval.file_diff",
  "approval.cancelled",
  "control_request",
  "llm.provider.oauth.auth",
  "llm.provider.oauth.device_code",
  "llm.provider.oauth.info",
  "llm.provider.oauth.progress",
  "artifact_content",
  "command_output_chunk",
  "image_chunk",
  "parent.notifications",
  "system_notice",
  "workspace.imported",
  "user_message.queue.updated",
  "turn.plan.updated",
  "turn.diff.updated",
  "inspector.update",
  "checkpoint.created",
  "checkpoint.list",
  "checkpoint.rewound",
  "checkpoint.run.list",
  "checkpoint.run.resume",
  "conversation.hydration.updated",
  "conversation.compaction.updated",
  "conversation.summary.updated",
  "goal.updated",
  "context_forked",
  "context_ledger",
  "context_side_query_result",
  "permission.rules.updated",
  "guidelines.updated",
  "file.changed",
  "git.pr_status",
  "diff.git_working_tree",
  "diff.git_staged",
  "diff.git_stage_file",
  "diff.git_unstage_file",
  "diff.git_stage_all",
  "diff.git_unstage_all",
  "diff.git_revert_file",
  "terminal.output",
  "terminal.exit",
  "terminal.created",
  "terminal.killed",
  "terminal.list",
  "terminal.snapshot",
  "terminal.resized",
  "background.started",
  "background.stalled",
  "background.completed",
  "stream_event",
  "rate_limit",
  "session.state_changed",
  "stream_resume",
  "preview.servers.updated",
  "preview.server.detected",
  "preview.server.stopped",
  "preview.navigated",
  "preview.refreshed",
  "preview.launch.config",
  "preview.launch.started",
  "preview.launch.stopped",
  "preview.server.ready",
  "preview.server.output",
  "preview.server.crashed",
  "preview.server.unhealthy",
  "preview.verified",
]);

const WORKSPACE_OWNED_EVENT_TYPES = new Set<ServerEventType>([
  // Generated artifacts and uploaded attachments may belong to a global,
  // projectless conversation. Their exact conversation owner is mandatory,
  // while workspace_root is allowed to be the canonical empty string. File,
  // Git, checkpoint, and preview-runtime events below still require both
  // ownership boundaries.
  "workspace.imported",
  "checkpoint.created",
  "checkpoint.list",
  "checkpoint.rewound",
  "checkpoint.run.list",
  "checkpoint.run.resume",
  "guidelines.updated",
  "file.changed",
  "git.pr_status",
  "diff.git_working_tree",
  "diff.git_staged",
  "diff.git_stage_file",
  "diff.git_unstage_file",
  "diff.git_stage_all",
  "diff.git_unstage_all",
  "diff.git_revert_file",
  "preview.servers.updated",
  "preview.server.detected",
  "preview.server.stopped",
  "preview.navigated",
  "preview.refreshed",
  "preview.launch.config",
  "preview.launch.started",
  "preview.launch.stopped",
  "preview.server.ready",
  "preview.server.output",
  "preview.server.crashed",
  "preview.server.unhealthy",
  "preview.verified",
]);

const hasKind = (value: unknown, kind: FieldKind): boolean => {
  if (kind === "array") return Array.isArray(value);
  if (kind === "record") return isRecord(value);
  if (kind === "number") return typeof value === "number" && Number.isFinite(value);
  return typeof value === kind;
};

const hasValidEnvelope = (value: Record<string, unknown>, type: string): boolean => {
  if (
    "seq" in value
    && (!Number.isSafeInteger(value.seq) || (value.seq as number) < 0)
  ) {
    console.warn("[ws] Dropping server event with invalid seq", type, value.seq);
    return false;
  }
  if (
    "previous_replay_seq" in value
    && (!Number.isSafeInteger(value.previous_replay_seq) || (value.previous_replay_seq as number) < 0)
  ) {
    console.warn(
      "[ws] Dropping server event with invalid previous_replay_seq",
      type,
      value.previous_replay_seq,
    );
    return false;
  }
  for (const [field, maximum] of [
    ["event_id", 1_024],
    ["task_id", 1_024],
    ["turn_id", 1_024],
    ["client_command_id", 1_024],
    ["client_command_type", 256],
  ] as const) {
    if (field in value && !isBoundedString(value[field], maximum)) {
      console.warn(`[ws] Dropping server event with invalid ${field}`, type, value[field]);
      return false;
    }
  }
  if (
    "timestamp" in value
    && (
      !isBoundedString(value.timestamp, 256)
      || !Number.isFinite(Date.parse(String(value.timestamp)))
    )
  ) {
    console.warn("[ws] Dropping server event with invalid timestamp", type, value.timestamp);
    return false;
  }
  return true;
};

const hasRequiredRoutingFields = (
  value: Record<string, unknown>,
  type: ServerEventType,
): boolean => {
  const contract = REQUIRED_ROUTING_FIELDS[type];
  if (!contract) return true;
  for (const [field, kind] of Object.entries(contract)) {
    if (!hasKind(value[field], kind)) {
      console.warn("[ws] Dropping server event with invalid routing field", type, field);
      return false;
    }
  }
  return true;
};

const isNonEmptyString = (value: unknown): value is string =>
  typeof value === "string" && value.trim().length > 0;

const hasOnlyNonEmptyStrings = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(isNonEmptyString);

const isNonNegativeSafeInteger = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

const isPositiveFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value > 0;

const isSafeHttpUrl = (value: unknown): value is string => {
  if (!isNonEmptyString(value) || value.length > 4_096) return false;
  try {
    const parsed = new URL(value);
    return (parsed.protocol === "http:" || parsed.protocol === "https:")
      && !parsed.username
      && !parsed.password;
  } catch {
    return false;
  }
};

const isProviderOAuthLink = (value: unknown): boolean => isRecord(value)
  && isSafeHttpUrl(value.url)
  && (!("label" in value) || isNonEmptyString(value.label));

const isProviderOAuthOption = (value: unknown): boolean => isRecord(value)
  && isNonEmptyString(value.id)
  && isNonEmptyString(value.label)
  && (!("description" in value) || isNonEmptyString(value.description));

const MAX_COMMAND_OUTPUT_CHARS = 1_048_576;
const MAX_STREAM_DELTA_CHARS = 1_048_576;
const MAX_MESSAGE_TEXT_CHARS = 4_194_304;
const MAX_EVENT_CONTENT_CHARS = 1_048_576;
const MAX_EVENT_SUMMARY_CHARS = 65_536;
const MAX_TERMINAL_OUTPUT_CHARS = 1_048_576;
const MAX_GENERATED_IMAGE_BASE64_CHARS = 10_485_760;
const MAX_NOTICE_TEXT_CHARS = 65_536;
const MAX_JSON_NODES = 4_096;
const MAX_JSON_DEPTH = 12;
const MAX_JSON_STRING_CHARS = 262_144;
const MAX_CONVERSATION_ITEMS = 4_096;
const MAX_TRANSCRIPT_MESSAGES = 4_096;
const MAX_SESSION_REPLAY_EVENTS = 1_000;
const MAX_STREAM_RESUME_BLOCKS = 1_024;
const MAX_STREAM_RESUME_TOOLS = 512;
const MAX_GOAL_TEXT_CHARS = 4_096;
const MAX_SESSION_PAYLOAD_NODES = 65_536;
const MAX_SESSION_PAYLOAD_DEPTH = 20;
const MAX_SESSION_PAYLOAD_STRING_CHARS = 16_777_216;
const RASTER_IMAGE_MEDIA_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);

const isBoundedString = (
  value: unknown,
  maximum: number,
  { allowEmpty = false }: { allowEmpty?: boolean } = {},
): value is string => typeof value === "string"
  && value.length <= maximum
  && (allowEmpty || value.trim().length > 0);

type JsonShapeBudget = {
  maxNodes?: number;
  maxDepth?: number;
  maxStringCharacters?: number;
  maxArrayItems?: number;
  maxObjectItems?: number;
};

const hasBoundedJsonShape = (
  value: unknown,
  budget: JsonShapeBudget = {},
): boolean => {
  const maxNodes = budget.maxNodes ?? MAX_JSON_NODES;
  const maxDepth = budget.maxDepth ?? MAX_JSON_DEPTH;
  const maxStringCharacters = budget.maxStringCharacters ?? MAX_JSON_STRING_CHARS;
  const maxArrayItems = budget.maxArrayItems ?? MAX_JSON_NODES;
  const maxObjectItems = budget.maxObjectItems ?? 1_024;
  const pending: Array<{ value: unknown; depth: number }> = [{ value, depth: 0 }];
  let nodes = 0;
  let stringCharacters = 0;
  while (pending.length > 0) {
    const current = pending.pop()!;
    nodes += 1;
    if (nodes > maxNodes || current.depth > maxDepth) return false;
    if (typeof current.value === "string") {
      stringCharacters += current.value.length;
      if (stringCharacters > maxStringCharacters) return false;
      continue;
    }
    if (
      current.value === null
      || typeof current.value === "boolean"
      || (typeof current.value === "number" && Number.isFinite(current.value))
    ) continue;
    if (Array.isArray(current.value)) {
      if (current.value.length > maxArrayItems) return false;
      if (nodes + pending.length + current.value.length > maxNodes) return false;
      for (const item of current.value) {
        pending.push({ value: item, depth: current.depth + 1 });
      }
      continue;
    }
    if (!isRecord(current.value)) return false;
    const entries = Object.entries(current.value);
    if (entries.length > maxObjectItems) return false;
    if (nodes + pending.length + entries.length > maxNodes) return false;
    for (const [key, item] of entries) {
      if (!isBoundedString(key, 1_024)) return false;
      stringCharacters += key.length;
      if (stringCharacters > maxStringCharacters) return false;
      pending.push({ value: item, depth: current.depth + 1 });
    }
  }
  return true;
};

const isWorkspaceRelativePath = (value: unknown): value is string => {
  if (!isBoundedString(value, 32_768) || value.includes("\0")) return false;
  const normalized = value.replace(/\\/g, "/");
  if (normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized)) return false;
  const segments = normalized.split("/");
  return segments.length > 0
    && segments.every((segment) => Boolean(segment) && segment !== "." && segment !== "..");
};

const isCommandAvailability = (value: unknown): boolean => isRecord(value)
  && isBoundedString(value.kind, 128)
  && isBoundedString(value.scope, 128)
  && (!("reason" in value) || isBoundedString(value.reason, 4_096));

const isCommandArgument = (value: unknown): boolean => isRecord(value)
  && isBoundedString(value.value, 512)
  && isBoundedString(value.description, 4_096, { allowEmpty: true });

const isCommandCatalogEntry = (value: unknown): boolean => {
  if (!isRecord(value)) return false;
  if (
    !isBoundedString(value.name, 512)
    || !isBoundedString(value.command, 512)
    || !isBoundedString(value.label, 1_024)
    || !isBoundedString(value.description, 16_384, { allowEmpty: true })
    || !["local", "template", "protocol"].includes(String(value.type))
    || !isBoundedString(value.source, 256)
    || typeof value.enabled !== "boolean"
    || !isCommandAvailability(value.availability)
  ) return false;
  const boundedOptionalStrings: ReadonlyArray<readonly [string, number, boolean?]> = [
    ["id", 1_024],
    ["kind", 256],
    ["panel", 256],
    ["extension_path", 32_768],
    ["source_path", 32_768],
    ["template", 1_048_576, true],
    ["search_text", 32_768, true],
    ["argument_hint", 4_096, true],
    ["base_dir", 32_768],
  ];
  if (boundedOptionalStrings.some(([field, maximum, allowEmpty]) => (
    field in value
    && !isBoundedString(value[field], maximum, { allowEmpty: allowEmpty === true })
  ))) return false;
  if ("is_skill_file" in value && typeof value.is_skill_file !== "boolean") return false;
  if ("args" in value) {
    if (!Array.isArray(value.args) || value.args.length > 128 || !value.args.every(isCommandArgument)) {
      return false;
    }
    const argumentValues = value.args.map((item) => String((item as Record<string, unknown>).value).toLowerCase());
    if (new Set(argumentValues).size !== argumentValues.length) return false;
  }
  if ("argument_names" in value) {
    if (
      !Array.isArray(value.argument_names)
      || value.argument_names.length > 128
      || !value.argument_names.every((item) => isBoundedString(item, 512))
    ) return false;
  }
  return true;
};

const isValidBase64Payload = (value: unknown): value is string => {
  if (!isBoundedString(value, MAX_GENERATED_IMAGE_BASE64_CHARS)) return false;
  if (value.length % 4 !== 0) return false;
  return /^[A-Za-z0-9+/]*={0,2}$/.test(value);
};

const hasMatchingRasterImageMagic = (value: string, mediaType: string): boolean => {
  try {
    const prefix = globalThis.atob(value.slice(0, Math.min(value.length, 24)));
    const byte = (index: number): number => prefix.charCodeAt(index);
    if (mediaType === "image/png") {
      return prefix.length >= 8
        && byte(0) === 0x89
        && prefix.slice(1, 4) === "PNG"
        && byte(4) === 0x0d
        && byte(5) === 0x0a
        && byte(6) === 0x1a
        && byte(7) === 0x0a;
    }
    if (mediaType === "image/jpeg") {
      return prefix.length >= 3
        && byte(0) === 0xff
        && byte(1) === 0xd8
        && byte(2) === 0xff;
    }
    if (mediaType === "image/gif") {
      return prefix.startsWith("GIF87a") || prefix.startsWith("GIF89a");
    }
    if (mediaType === "image/webp") {
      return prefix.length >= 12
        && prefix.slice(0, 4) === "RIFF"
        && prefix.slice(8, 12) === "WEBP";
    }
    return false;
  } catch {
    return false;
  }
};

const isCheckpointOrigin = (value: unknown): boolean => isRecord(value)
  && isBoundedString(value.run_id, 1_024)
  && isBoundedString(value.conversation_id, 1_024)
  && isBoundedString(value.session_id, 1_024)
  && isNonNegativeSafeInteger(value.sequence)
  && typeof value.timestamp === "number"
  && Number.isFinite(value.timestamp)
  && value.timestamp >= 0
  && isBoundedString(value.stopped_reason, 16_384, { allowEmpty: true });

const isWorkspaceProject = (value: unknown): value is Record<string, unknown> => isRecord(value)
  && isBoundedString(value.root_path, 32_768)
  && isBoundedString(value.project_type, 1_024)
  && isBoundedString(value.name, 4_096)
  && isBoundedString(value.description, 65_536, { allowEmpty: true })
  && isNonNegativeSafeInteger(value.file_count)
  && isNonNegativeSafeInteger(value.total_size)
  && typeof value.has_project_instructions === "boolean"
  && typeof value.index_truncated === "boolean";

const CONTEXT_LEDGER_CATEGORIES = new Set([
  "system_runtime",
  "guidelines",
  "skills",
  "files_attachments",
  "history",
  "tool_results",
  "memory",
  "compaction_summaries",
]);

const isContextLedgerEntry = (value: unknown): boolean => {
  if (!isRecord(value)) return false;
  return typeof value.category === "string"
    && CONTEXT_LEDGER_CATEGORIES.has(value.category)
    && isBoundedString(value.label, 4_096)
    && isNonNegativeSafeInteger(value.estimated_tokens)
    && isNonNegativeSafeInteger(value.item_count)
    && isNonNegativeSafeInteger(value.source_count)
    && Array.isArray(value.sources)
    && value.sources.length <= 256
    && value.sources.every((source) => isBoundedString(source, 32_768))
    && new Set(value.sources).size === value.sources.length;
};

const isContextLedgerPayload = (value: unknown): boolean => isRecord(value)
  && value.schema_version === 1
  && isNonNegativeSafeInteger(value.estimated_tokens)
  && isNonNegativeSafeInteger(value.actual_tokens)
  && isNonNegativeSafeInteger(value.compaction_count)
  && isNonNegativeSafeInteger(value.native_attachment_tokens)
  && isNonNegativeSafeInteger(value.native_attachment_count)
  && Array.isArray(value.entries)
  && value.entries.length <= 64
  && value.entries.every(isContextLedgerEntry);

const isAgentMessageItem = (
  value: unknown,
  lifecycle: "started" | "completed",
): value is Record<string, unknown> => {
  if (!isRecord(value)) return false;
  if (
    !isBoundedString(value.id, 1_024)
    || value.type !== "agent_message"
    || !isBoundedString(value.text, MAX_MESSAGE_TEXT_CHARS, { allowEmpty: true })
  ) return false;
  if (lifecycle === "started") {
    return value.text === ""
      && value.status === "in_progress"
      && (!("source" in value) || isBoundedString(value.source, 256));
  }
  return ["completed", "partial", "cancelled", "failed"].includes(String(value.status))
    && isBoundedString(value.source, 256);
};

const isReplyAttachment = (value: unknown): boolean => isRecord(value)
  && isBoundedString(value.path, 32_768)
  && !String(value.path).includes("\0")
  && isNonNegativeSafeInteger(value.size)
  && typeof value.is_image === "boolean";

/**
 * Validate the small metadata records carried alongside a tool result.
 *
 * The artifact body is deliberately not transported in a tool_result event;
 * only its owner-scoped id and descriptive metadata are.  Keep the legacy
 * fields optional so older transcripts that only contain artifact_id continue
 * to hydrate, while rejecting malformed values when a field is present.
 */
const isToolOutputFile = (value: unknown): boolean => {
  if (!isRecord(value) || !isBoundedString(value.path, 32_768) || String(value.path).includes("\0")) {
    return false;
  }
  return (!('size' in value) || isNonNegativeSafeInteger(value.size))
    && (!('name' in value) || isBoundedString(value.name, 512))
    && (!('mime_type' in value) || isBoundedString(value.mime_type, 128))
    && (!('is_image' in value) || typeof value.is_image === "boolean");
};

const isNonNegativeNumberRecord = (value: unknown): value is Record<string, number> => {
  if (!isRecord(value)) return false;
  const entries = Object.entries(value);
  return entries.length <= 128
    && entries.every(([name, amount]) => (
      isBoundedString(name, 256)
      && isNonNegativeSafeInteger(amount)
    ));
};

const AGENT_PROGRESS_STAGE_SET = new Set<string>(AGENT_PROGRESS_STAGES);
const AGENT_PROGRESS_STATUS_SET = new Set<string>(AGENT_PROGRESS_STATUSES);
const AGENT_PROGRESS_PHASE_SET = new Set<string>(AGENT_PROGRESS_PHASES);
const AGENT_PROGRESS_PROVIDER_STATE_SET = new Set<string>(AGENT_PROGRESS_PROVIDER_STATES);
const RUNTIME_SPAN_STATUSES = new Set([
  "running",
  "completed",
  "failed",
  "cancelled",
  "interrupted",
  "superseded",
  "partial",
  "info",
]);
const TOOL_RUNTIME_SPAN_EVENTS = new Set([
  "tool.preparing",
  "tool.queued",
  "approval.waiting",
  "tool.started",
  "tool.first_output",
  "tool.completed",
]);

const hasValidControlRequest = (value: Record<string, unknown>): boolean => {
  const requestId = value.request_id;
  const request = value.request;
  if (!isNonEmptyString(requestId) || !isRecord(request)) return false;
  for (const field of [
    "turn_id",
    "message_id",
    "workspace_root",
    "permission_mode",
    "workspace_scope",
  ]) {
    if (field in value && !isNonEmptyString(value[field])) return false;
  }
  if (
    ("timeout_seconds" in value && (
      typeof value.timeout_seconds !== "number"
      || !Number.isFinite(value.timeout_seconds)
      || value.timeout_seconds <= 0
    ))
    || ("expires_at" in value && (
      typeof value.expires_at !== "number"
      || !Number.isFinite(value.expires_at)
      || value.expires_at <= 0
    ))
  ) return false;

  if (request.subtype === "can_use_tool") {
    return isNonEmptyString(request.tool_name)
      && isRecord(request.input)
      // Tool arguments and diffs are attacker-influenced and unbounded at the
      // source; a control request carrying either must fit the same JSON budget
      // every other projected payload does.
      && hasBoundedJsonShape(request.input)
      && request.tool_use_id === requestId
      && (!("diff" in request) || (
        isBoundedString(request.diff, MAX_TERMINAL_OUTPUT_CHARS, { allowEmpty: true })
        || (isRecord(request.diff) && hasBoundedJsonShape(request.diff))
      ))
      && ["source_agent", "source_thread", "source_tool"].every((field) => (
        !(field in request) || isNonEmptyString(request[field])
      ));
  }
  if (request.subtype === "elicitation") {
    return request.tool_use_id === requestId
      && isNonEmptyString(request.prompt)
      && isNonEmptyString(request.question)
      && (!("schema" in request) || (isRecord(request.schema) && hasBoundedJsonShape(request.schema)))
      && ["options", "choices", "allowed_values"].every((field) => (
        !(field in request) || (Array.isArray(request[field]) && hasBoundedJsonShape(request[field]))
      ));
  }
  if (request.subtype === "provider_auth_prompt") {
    const promptType = request.prompt_type;
    const options = request.options;
    const validBase = isNonEmptyString(request.prompt)
      && isNonEmptyString(request.provider)
      && ["text", "secret", "select", "manual_code"].includes(String(promptType))
      && typeof request.allow_empty === "boolean"
      && typeof request.allow_custom === "boolean"
      && (!("placeholder" in request) || isNonEmptyString(request.placeholder));
    if (!validBase) return false;
    if (promptType === "select") {
      if (request.allow_empty || request.allow_custom) return false;
      if (!Array.isArray(options) || options.length === 0 || options.length > 64) return false;
      if (!options.every(isProviderOAuthOption)) return false;
      const ids = options.map((option) => String((option as Record<string, unknown>).id));
      return new Set(ids).size === ids.length;
    }
    return options === undefined && request.allow_custom === true;
  }
  return false;
};

const hasOptionalKind = (
  value: Record<string, unknown>,
  field: string,
  kind: FieldKind,
): boolean => !(field in value) || hasKind(value[field], kind);

const isCheckpointRecord = (
  value: unknown,
  expectedConversationId?: unknown,
  expectedWorkspaceRoot?: unknown,
): value is Record<string, unknown> => {
  if (!isRecord(value)) return false;
  const valid = isBoundedString(value.id, 1_024)
    && isBoundedString(value.conversation_id, 1_024)
    && isBoundedString(value.session_id, 1_024)
    && isBoundedString(value.tool_call_id, 1_024)
    && isBoundedString(value.tool_name, 1_024)
    && isBoundedString(value.workspace_root, 32_768)
    && isBoundedString(value.created_at, 128)
    && Number.isFinite(Date.parse(value.created_at))
    && Array.isArray(value.paths)
    && value.paths.length > 0
    && value.paths.length <= 4_096
    && value.paths.every(isWorkspaceRelativePath)
    && isRecord(value.metadata)
    && hasBoundedJsonShape(value.metadata);
  if (!valid) return false;
  if (
    isNonEmptyString(expectedConversationId)
    && value.conversation_id !== expectedConversationId
  ) return false;
  if (
    isNonEmptyString(expectedWorkspaceRoot)
    && normalizeWorkspaceRoot(value.workspace_root) !== normalizeWorkspaceRoot(expectedWorkspaceRoot)
  ) return false;
  return true;
};

const isRecentWorkspace = (value: unknown): boolean => {
  if (!isRecord(value)) return false;
  return typeof value.path === "string"
    && typeof value.name === "string"
    && typeof value.project_type === "string"
    && typeof value.last_opened === "number"
    && Number.isFinite(value.last_opened);
};

const isPermissionRule = (
  value: unknown,
  required: readonly string[],
): boolean => isRecord(value) && required.every((field) => typeof value[field] === "string");

const hasValidPermissionRules = (value: unknown): boolean => {
  if (!isRecord(value)) return false;
  if (typeof value.mode !== "string" || typeof value.context_source !== "string") return false;
  const contracts: ReadonlyArray<readonly [string, readonly string[]]> = [
    ["system_deny", ["pattern", "source"]],
    ["session_deny", ["pattern", "source"]],
    ["session_overrides", ["pattern", "level", "source"]],
    ["session_prompt_rules", ["tool", "rule_content", "behavior", "destination", "source"]],
  ];
  return contracts.every(([field, required]) => (
    Array.isArray(value[field])
    && value[field].every((rule) => isPermissionRule(rule, required))
  ));
};

const isOptionalBoundedText = (
  value: Record<string, unknown>,
  field: string,
  maximum: number,
  { allowNull = true, allowEmpty = true }: { allowNull?: boolean; allowEmpty?: boolean } = {},
): boolean => {
  if (!(field in value) || value[field] === undefined) return true;
  if (value[field] === null) return allowNull;
  return isBoundedString(value[field], maximum, { allowEmpty });
};

const isOptionalIsoTimestamp = (
  value: Record<string, unknown>,
  field: string,
): boolean => {
  if (!(field in value) || value[field] === undefined || value[field] === null) return true;
  if (!isBoundedString(value[field], 256, { allowEmpty: true })) return false;
  const text = String(value[field]).trim();
  return !text || Number.isFinite(Date.parse(text));
};

const isBoundedRecordArray = (
  value: unknown,
  maximum: number,
  budget: JsonShapeBudget = {},
): value is Record<string, unknown>[] => Array.isArray(value)
  && value.length <= maximum
  && value.every((item) => isRecord(item) && hasBoundedJsonShape(item, budget));

const isGoalPayload = (value: unknown): value is Record<string, unknown> => {
  if (!isRecord(value) || !hasBoundedJsonShape(value)) return false;
  if (!isOptionalBoundedText(value, "id", 1_024)) return false;
  if (!isOptionalBoundedText(value, "text", MAX_GOAL_TEXT_CHARS)) return false;
  if (!isOptionalBoundedText(value, "status", 64)) return false;
  if (!isOptionalBoundedText(value, "created_at", 256)) return false;
  if (!isOptionalBoundedText(value, "updated_at", 256)) return false;
  if (!isOptionalBoundedText(value, "source", 256)) return false;
  if (!isOptionalIsoTimestamp(value, "created_at") || !isOptionalIsoTimestamp(value, "updated_at")) return false;
  if ("status" in value && value.status !== null && value.status !== undefined
    && !["active", "paused"].includes(String(value.status))) return false;
  return true;
};

const isConversationSummaryPayload = (
  value: unknown,
): value is Record<string, unknown> => {
  if (!isRecord(value)
    || !isNonEmptyString(value.id)
    || !hasBoundedJsonShape(value, {
      maxNodes: MAX_SESSION_PAYLOAD_NODES,
      maxDepth: MAX_SESSION_PAYLOAD_DEPTH,
      maxStringCharacters: MAX_SESSION_PAYLOAD_STRING_CHARS,
      maxArrayItems: 8_192,
      maxObjectItems: 4_096,
    })) return false;

  for (const [field, maximum] of [
    ["title", 4_096],
    ["created_at", 256],
    ["updated_at", 256],
    ["archived_at", 256],
    ["workspace_root", 32_768],
    ["git_branch", 4_096],
    ["worktree_path", 32_768],
    ["summary", 65_536],
    ["parent_conversation_id", 1_024],
    ["fork_id", 1_024],
    ["branch_kind", 256],
    ["merged_into_conversation_id", 1_024],
    ["merged_at", 256],
  ] as const) {
    if (!isOptionalBoundedText(value, field, maximum)) return false;
  }
  for (const field of ["created_at", "updated_at", "archived_at", "merged_at"]) {
    if (!isOptionalIsoTimestamp(value, field)) return false;
  }
  if ("conversation_type" in value && value.conversation_type !== undefined && value.conversation_type !== null
    && !["main", "side_chat"].includes(String(value.conversation_type))) return false;
  if ("memory_mode" in value && value.memory_mode !== undefined && value.memory_mode !== null
    && !["enabled", "disabled", "polluted"].includes(String(value.memory_mode))) return false;
  for (const field of ["memory_polluted", "archived", "git_isolated"]) {
    if (field in value && typeof value[field] !== "boolean") return false;
  }
  for (const field of ["revision", "message_count", "parent_message_index"]) {
    if (field in value && value[field] !== null && value[field] !== undefined
      && !isNonNegativeSafeInteger(value[field])) return false;
  }
  if ("memory_pollution_sources" in value && value.memory_pollution_sources !== undefined && value.memory_pollution_sources !== null) {
    if (!Array.isArray(value.memory_pollution_sources)
      || value.memory_pollution_sources.length > 256
      || !hasOnlyNonEmptyStrings(value.memory_pollution_sources)
      || value.memory_pollution_sources.some((source) => !isBoundedString(source, 1_024))) return false;
  }
  if ("goal" in value && value.goal !== null && value.goal !== undefined && !isGoalPayload(value.goal)) return false;
  return true;
};

const isConversationRecordPayload = (
  value: unknown,
): value is Record<string, unknown> => {
  if (!isConversationSummaryPayload(value)) return false;
  for (const field of ["transcript", "messages"]) {
    if (!(field in value) || value[field] === undefined || value[field] === null) continue;
    if (!isBoundedRecordArray(value[field], MAX_TRANSCRIPT_MESSAGES, {
      maxNodes: 16_384,
      maxDepth: 18,
      maxStringCharacters: 4_194_304,
      maxArrayItems: 8_192,
      maxObjectItems: 4_096,
    })) return false;
  }
  if ("permission_deny_rules" in value && value.permission_deny_rules !== undefined && value.permission_deny_rules !== null) {
    if (!Array.isArray(value.permission_deny_rules)
      || value.permission_deny_rules.length > 512
      || !value.permission_deny_rules.every((item) => isBoundedString(item, 4_096))) return false;
  }
  if ("permission_overrides" in value && value.permission_overrides !== undefined && value.permission_overrides !== null) {
    if (!isRecord(value.permission_overrides) || !hasBoundedJsonShape(value.permission_overrides)) return false;
  }
  if ("context_snapshot" in value && value.context_snapshot !== undefined && value.context_snapshot !== null
    && (!isRecord(value.context_snapshot) || !hasBoundedJsonShape(value.context_snapshot, {
      maxNodes: 32_768,
      maxDepth: 20,
      maxStringCharacters: 8_388_608,
      maxArrayItems: 8_192,
      maxObjectItems: 4_096,
    }))) return false;
  return true;
};

const isSessionWorkspacePayload = (value: unknown): boolean => {
  if (value === null || value === undefined) return true;
  if (!isRecord(value) || !hasBoundedJsonShape(value)) return false;
  return isOptionalBoundedText(value, "root_path", 32_768)
    && isOptionalBoundedText(value, "name", 4_096);
};

const isRuntimeSessionSnapshot = (value: unknown): value is Record<string, unknown> => {
  if (!isRecord(value) || !hasBoundedJsonShape(value, {
    maxNodes: MAX_SESSION_PAYLOAD_NODES,
    maxDepth: MAX_SESSION_PAYLOAD_DEPTH,
    maxStringCharacters: MAX_SESSION_PAYLOAD_STRING_CHARS,
    maxArrayItems: 8_192,
    maxObjectItems: 4_096,
  })) return false;
  for (const field of [
    "session_id",
    "parent_session_id",
    "active_conversation_id",
    "active_task_id",
    "workspace_root",
    "selected_model",
    "permission_mode",
    "permission_profile",
    "permission_source",
    "workspace_scope",
  ]) {
    if (!isOptionalBoundedText(value, field, field === "workspace_root" ? 32_768 : 4_096)) return false;
  }
  for (const field of ["active_stream_conversation_ids", "invoked_skill_names"]) {
    if (field in value && value[field] !== undefined && value[field] !== null
      && (!Array.isArray(value[field]) || value[field].length > MAX_CONVERSATION_ITEMS
        || !value[field].every((item) => isBoundedString(item, 4_096)))) return false;
  }
  for (const field of ["pending_approvals", "queued_user_messages", "pending_turn_inputs", "forks", "running_tasks"]) {
    if (field in value && value[field] !== undefined && value[field] !== null
      && !isBoundedRecordArray(value[field], MAX_CONVERSATION_ITEMS, {
        maxNodes: 8_192,
        maxDepth: 16,
        maxStringCharacters: 2_097_152,
        maxArrayItems: 4_096,
        maxObjectItems: 2_048,
      })) return false;
  }
  if ("pending_approval_count" in value && !isNonNegativeSafeInteger(value.pending_approval_count)) return false;
  if ("active_conversation" in value && value.active_conversation !== null && value.active_conversation !== undefined
    && !isConversationRecordPayload(value.active_conversation)) return false;
  if ("task_summary" in value && value.task_summary !== null && value.task_summary !== undefined
    && (!isRecord(value.task_summary) || !hasBoundedJsonShape(value.task_summary))) return false;
  if ("capabilities" in value && value.capabilities !== null && value.capabilities !== undefined
    && (!isRecord(value.capabilities) || !hasBoundedJsonShape(value.capabilities))) return false;
  if ("provider_capabilities" in value && value.provider_capabilities !== null && value.provider_capabilities !== undefined
    && (!isRecord(value.provider_capabilities) || !hasBoundedJsonShape(value.provider_capabilities))) return false;
  const activeId = typeof value.active_conversation_id === "string" ? value.active_conversation_id.trim() : "";
  const active = isRecord(value.active_conversation) ? String(value.active_conversation.id ?? "").trim() : "";
  return !activeId || !active || activeId === active;
};

const isStreamResumeToolRecord = (value: unknown, pending: boolean): value is Record<string, unknown> => {
  if (!isRecord(value) || !hasBoundedJsonShape(value)) return false;
  if (!isBoundedString(value.id, 1_024) || !isBoundedString(value.name, 1_024)) return false;
  if (pending && (!isRecord(value.args) || !hasBoundedJsonShape(value.args))) return false;
  if (!pending && "args" in value && value.args !== undefined && value.args !== null
    && (!isRecord(value.args) || !hasBoundedJsonShape(value.args))) return false;
  for (const field of ["status", "transition", "waiting_on", "waitingOn", "blocking_reason", "blockingReason", "outputPreview", "stdoutPreview", "stderrPreview", "display_hint", "displayHint", "input_summary", "inputSummary", "iteration_id", "iterationId", "phase", "visibility"]) {
    if (!isOptionalBoundedText(value, field, field.toLowerCase().includes("preview") ? MAX_COMMAND_OUTPUT_CHARS : 4_096)) return false;
  }
  for (const field of ["started_at", "startedAt", "finished_at", "finishedAt", "duration_ms", "durationMs"]) {
    if (field in value && value[field] !== undefined && value[field] !== null && !isNonNegativeSafeInteger(value[field])) return false;
  }
  return true;
};

const isStreamResumeBlock = (value: unknown): value is Record<string, unknown> => {
  if (!isRecord(value) || !hasBoundedJsonShape(value, {
    maxNodes: 8_192,
    maxDepth: 18,
    // A reconnect snapshot may contain the accumulated final-answer block,
    // whose canonical item contract is larger than a single streaming delta.
    // Keep this aligned with item.completed instead of rejecting a legitimate
    // long answer only during recovery.
    // The answer itself may consume the full canonical message budget. Leave
    // bounded room for block keys and metadata, then enforce the content field
    // independently so that this allowance cannot enlarge the answer payload.
    maxStringCharacters: MAX_MESSAGE_TEXT_CHARS + MAX_EVENT_SUMMARY_CHARS,
    maxArrayItems: 4_096,
    maxObjectItems: 2_048,
  })) return false;
  if (!isBoundedString(value.type, 128)) return false;
  if ("content" in value && !isBoundedString(value.content, MAX_MESSAGE_TEXT_CHARS, { allowEmpty: true })) {
    return false;
  }
  return true;
};

const hasValidSemanticPayload = (
  value: Record<string, unknown>,
  type: ServerEventType,
): boolean => {
  let valid = true;
  if (type === "agent_message.delta") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.item_id, 1_024)
      && typeof value.delta === "string"
      && value.delta.length > 0
      && value.delta.length <= MAX_STREAM_DELTA_CHARS
      && (!("message_id" in value) || isBoundedString(value.message_id, 1_024));
  }
  if (type === "item.started") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && isAgentMessageItem(value.item, "started");
  }
  if (type === "item.completed") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && isAgentMessageItem(value.item, "completed")
      && (!("finish_reason" in value) || isBoundedString(value.finish_reason, 16_384))
      && (!("provider_raw" in value) || (
        isRecord(value.provider_raw)
        && hasBoundedJsonShape(value.provider_raw)
      ))
      && (!("attachments" in value) || (
        Array.isArray(value.attachments)
        && value.attachments.length <= 512
        && value.attachments.every(isReplyAttachment)
      ))
      && !("tool_calls" in value);
  }
  if (type === "thinking_delta" || type === "thinking") {
    const lifecycle = value.lifecycle === undefined ? "delta" : value.lifecycle;
    const allowsEmpty = lifecycle === "start" || lifecycle === "end";
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && typeof value.content === "string"
      && value.content.length <= MAX_STREAM_DELTA_CHARS
      && (allowsEmpty || value.content.length > 0)
      && ["start", "delta", "end"].includes(String(lifecycle))
      && (!("source" in value) || isBoundedString(value.source, 256))
      && (!("visibility" in value) || [
        "timeline",
        "compact",
        "debug",
        "hidden",
        "internal",
        "redacted",
      ].includes(String(value.visibility)))
      && (!("phase" in value) || isBoundedString(value.phase, 64))
      && (!("provider_reasoning_type" in value) || isBoundedString(value.provider_reasoning_type, 128))
      && (!("item_id" in value) || isBoundedString(value.item_id, 1_024))
      && (!("content_index" in value) || isNonNegativeSafeInteger(value.content_index));
  }
  if (type === "tool_call") {
    valid = valid
      && (!("visibility" in value) || ["timeline", "compact", "debug"].includes(String(value.visibility)));
  }
  if (type === "tool_result") {
    const retryableArtifactFields = [
      ["artifact_id", 1_024],
      ["artifact_kind", 64],
      ["artifact_media_type", 128],
    ] as const;
    valid = valid
      // summary is required by the routing contract. Empty summaries are
      // retained for compatibility with a few legacy blocked-tool events.
      && isBoundedString(value.summary, MAX_EVENT_SUMMARY_CHARS, { allowEmpty: true })
      && retryableArtifactFields.every(([field, maximum]) => (
        !(field in value) || isBoundedString(value[field], maximum)
      ))
      && (!('artifact_bytes' in value) || isNonNegativeSafeInteger(value.artifact_bytes))
      && (!('is_error' in value) || typeof value.is_error === "boolean")
      && (!('visibility' in value) || ["timeline", "compact", "debug"].includes(String(value.visibility)))
      && (!('output_files' in value) || (
        Array.isArray(value.output_files)
        && value.output_files.length <= 2_048
        && value.output_files.every(isToolOutputFile)
      ));
  }
  if (type === "agent.item") {
    const status = String(value.status ?? "completed");
    const visibility = String(value.visibility ?? "timeline");
    const hasContent = isBoundedString(value.content, MAX_EVENT_CONTENT_CHARS);
    const hasSummary = isBoundedString(value.summary, MAX_EVENT_SUMMARY_CHARS);
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && isBoundedString(value.id, 1_024)
      && (!("item_id" in value) || value.item_id === value.id)
      && isBoundedString(value.kind, 256)
      && ["running", "completed", "partial", "failed", "cancelled", "info", "retracted"].includes(status)
      && ["timeline", "compact", "debug"].includes(visibility)
      && (status === "retracted" || visibility === "debug" || hasContent || hasSummary)
      && (!("content" in value) || isBoundedString(value.content, MAX_EVENT_CONTENT_CHARS, { allowEmpty: true }))
      && (!("summary" in value) || isBoundedString(value.summary, MAX_EVENT_SUMMARY_CHARS))
      && [
        ["loop_id", 1_024],
        ["iteration_id", 1_024],
        ["parent_id", 1_024],
        ["role", 128],
        ["source", 256],
        ["title", MAX_EVENT_SUMMARY_CHARS],
        ["group_id", 1_024],
        ["step_id", 1_024],
        ["skill_name", 1_024],
        ["trigger_mode", 64],
        ["source_level", 1_024],
        ["reason", 16_384],
      ].every(([field, maximum]) => (
        !((field as string) in value)
        || isBoundedString(value[field as string], maximum as number)
      ))
      && (!("created_at" in value) || isNonNegativeSafeInteger(value.created_at))
      && (!("order" in value) || isNonNegativeSafeInteger(value.order))
      && (!("token_estimate" in value) || isNonNegativeSafeInteger(value.token_estimate))
      && (!("default_collapsed" in value) || typeof value.default_collapsed === "boolean")
      && (!("tool_call_ids" in value) || (
        Array.isArray(value.tool_call_ids)
        && value.tool_call_ids.length <= 256
        && value.tool_call_ids.every((item) => isBoundedString(item, 1_024))
        && new Set(value.tool_call_ids).size === value.tool_call_ids.length
      ));
  }
  if (type === "agent.progress") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && isBoundedString(value.id, 1_024)
      && AGENT_PROGRESS_STAGE_SET.has(String(value.stage))
      && AGENT_PROGRESS_STATUS_SET.has(String(value.status))
      && isBoundedString(value.message, MAX_EVENT_SUMMARY_CHARS)
      && AGENT_PROGRESS_PHASE_SET.has(String(value.phase))
      && ["timeline", "compact", "debug"].includes(String(value.visibility))
      && (!("label" in value) || isBoundedString(value.label, 4_096))
      && (!("summary" in value) || isBoundedString(value.summary, MAX_EVENT_SUMMARY_CHARS))
      && (!("detail" in value) || isBoundedString(value.detail, MAX_EVENT_SUMMARY_CHARS))
      && (!("tool_call_id" in value) || isBoundedString(value.tool_call_id, 1_024))
      && (!("tool_name" in value) || isBoundedString(value.tool_name, 1_024))
      && (!("tool_call_id" in value) || isBoundedString(value.tool_name, 1_024))
      && ["group_id", "step_id", "iteration_id"].every((field) => (
        !(field in value) || isBoundedString(value[field], 1_024)
      ))
      && (!("count" in value) || isNonNegativeSafeInteger(value.count))
      && (!("retry_attempt" in value) || isNonNegativeSafeInteger(value.retry_attempt))
      && (!("max_retries" in value) || isNonNegativeSafeInteger(value.max_retries))
      && (!("retry_attempt" in value) || !("max_retries" in value)
        || Number(value.retry_attempt) <= Number(value.max_retries))
      && (!("retry_after_ms" in value) || isNonNegativeSafeInteger(value.retry_after_ms))
      && (!("error_message" in value) || isBoundedString(value.error_message, MAX_EVENT_SUMMARY_CHARS))
      && (!("operation_id" in value) || isBoundedString(value.operation_id, 1_024))
      && (!("provider_state" in value) || AGENT_PROGRESS_PROVIDER_STATE_SET.has(String(value.provider_state)))
      && (!("ephemeral" in value) || typeof value.ephemeral === "boolean");
  }
  if (type === "runtime.span") {
    const startedAt = value.started_at;
    const endedAt = value.ended_at;
    const durationMs = value.duration_ms;
    const hasToolCallId = "tool_call_id" in value;
    const hasToolName = "tool_name" in value;
    const hasCompleteTiming = startedAt !== undefined && endedAt !== undefined;
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && isBoundedString(value.event, 256)
      && isBoundedString(value.span_id, 1_024)
      && RUNTIME_SPAN_STATUSES.has(String(value.status))
      && typeof value.ui_visible === "boolean"
      && typeof value.debug_only === "boolean"
      && [
        ["parent_span_id", 1_024],
        ["run_id", 1_024],
        ["turn_id", 1_024],
        ["iteration_id", 1_024],
        ["phase", 64],
        ["label", 4_096],
        ["summary", MAX_EVENT_SUMMARY_CHARS],
        ["tool_call_id", 1_024],
        ["tool_name", 1_024],
        ["agent_id", 1_024],
        ["waiting_on", 1_024],
        ["blocking_reason", 16_384],
      ].every(([field, maximum]) => (
        !((field as string) in value)
        || isBoundedString(value[field as string], maximum as number)
      ))
      && (!hasToolCallId || (
        hasToolName
        && TOOL_RUNTIME_SPAN_EVENTS.has(String(value.event))
        && ["tool", "approval"].includes(String(value.phase))
      ))
      && (!hasToolName || hasToolCallId)
      && (startedAt === undefined || isNonNegativeSafeInteger(startedAt))
      && (endedAt === undefined || isNonNegativeSafeInteger(endedAt))
      && (durationMs === undefined || isNonNegativeSafeInteger(durationMs))
      && (!hasCompleteTiming || Number(endedAt) >= Number(startedAt))
      && (!hasCompleteTiming || durationMs === undefined || durationMs === Number(endedAt) - Number(startedAt))
      && (!("data" in value) || (
        isRecord(value.data)
        && hasBoundedJsonShape(value.data)
      ));
  }
  if (type === "done") {
    const usage = value.usage;
    const status = String(value.status);
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && ["completed", "partial", "failed", "cancelled", "interrupted"].includes(status)
      && isRecord(usage)
      && [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
      ].every((field) => isNonNegativeSafeInteger(usage[field]))
      && typeof usage.input_includes_cache_read === "boolean"
      && (!("input_includes_cache_write" in usage)
        || typeof usage.input_includes_cache_write === "boolean")
      && [
        "cache_deleted_input_tokens",
        "ordinary_input_tokens",
        "prompt_cache_total_tokens",
        "reasoning_output_tokens",
      ].every((field) => !(field in usage) || isNonNegativeSafeInteger(usage[field]))
      && (!("prompt_cache_hit_rate" in usage) || (
        typeof usage.prompt_cache_hit_rate === "number"
        && Number.isFinite(usage.prompt_cache_hit_rate)
        && usage.prompt_cache_hit_rate >= 0
        && usage.prompt_cache_hit_rate <= 100
      ))
      && (!("cost_usd" in usage) || (
        typeof usage.cost_usd === "number"
        && Number.isFinite(usage.cost_usd)
        && usage.cost_usd >= 0
      ))
      && (!("reason" in value) || isBoundedString(value.reason, 16_384))
      && (!("duration_ms" in value) || isNonNegativeSafeInteger(value.duration_ms))
      && (!("failure_recoverable" in value) || (
        status === "failed"
        && typeof value.failure_recoverable === "boolean"
      ))
      && (!("provider_raw" in value) || (
        isRecord(value.provider_raw) && hasBoundedJsonShape(value.provider_raw)
      ));
  }
  if (type === "error") {
    valid = isBoundedString(value.message, MAX_EVENT_SUMMARY_CHARS)
      && typeof value.recoverable === "boolean"
      && isBoundedString(value.error_type, 256)
      && [
        ["conversation_id", 1_024],
        ["message_id", 1_024],
        ["tool_call_id", 1_024],
        ["request_id", 1_024],
        ["error_code", 256],
        ["provider_error_type", 256],
        ["provider", 256],
      ].every(([field, maximum]) => (
        !((field as string) in value)
        || isBoundedString(value[field as string], maximum as number)
      ))
      && (!("attachments" in value) || (
        Array.isArray(value.attachments)
        && value.attachments.length <= 512
        && hasBoundedJsonShape(value.attachments)
        && value.attachments.every((attachment) => (
          isRecord(attachment)
        ))
      ));
  }
  if (type === "approval.cancelled") {
    const requestIds = value.request_ids;
    valid = isBoundedString(value.conversation_id, 1_024)
      && Array.isArray(requestIds)
      && requestIds.length > 0
      && requestIds.length <= 512
      && requestIds.every((requestId) => isBoundedString(requestId, 1_024))
      && new Set(requestIds).size === requestIds.length
      && (!('reason' in value) || isBoundedString(value.reason, 256));
  }
  if (type === "stream_resume") {
    const pending = value.tool_calls_pending;
    const states = value.tool_states;
    const blocks = value.content_blocks;
    const pendingIds = Array.isArray(pending)
      ? pending.map((item) => isRecord(item) ? String(item.id ?? "").trim() : "")
      : [];
    const stateIds = Array.isArray(states)
      ? states.map((item) => isRecord(item) ? String(item.id ?? "").trim() : "")
      : [];
    valid = isBoundedString(value.conversation_id, 1_024)
      && (value.message_id === null || isBoundedString(value.message_id, 1_024))
      && Array.isArray(pending)
      && pending.length <= MAX_STREAM_RESUME_TOOLS
      && pending.every((item) => isStreamResumeToolRecord(item, true))
      && pendingIds.every(Boolean)
      && new Set(pendingIds).size === pendingIds.length
      && (!('tool_states' in value) || (
        Array.isArray(states)
        && states.length <= MAX_STREAM_RESUME_TOOLS
        && states.every((item) => isStreamResumeToolRecord(item, false))
        && stateIds.every(Boolean)
        && new Set(stateIds).size === stateIds.length
      ))
      && (!('content_blocks' in value) || (
        Array.isArray(blocks)
        && blocks.length <= MAX_STREAM_RESUME_BLOCKS
        && blocks.every(isStreamResumeBlock)
      ))
      && (!('turn_id' in value) || isBoundedString(value.turn_id, 1_024))
      && (!('phase' in value) || isBoundedString(value.phase, 128, { allowEmpty: true }))
      && (!('stream_status' in value) || isBoundedString(value.stream_status, 128, { allowEmpty: true }))
      && (!('last_event_type' in value) || isBoundedString(value.last_event_type, 256, { allowEmpty: true }))
      && (!('event_seq' in value) || isNonNegativeSafeInteger(value.event_seq));
  }
  if (type === "goal.updated") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isGoalPayload(value.goal)
      && (!('source' in value) || isBoundedString(value.source, 256))
      && isOptionalIsoTimestamp(value, "updated_at")
      && (!('revision' in value) || isNonNegativeSafeInteger(value.revision));
  }
  if (type === "conversation.list") {
    const conversations = value.conversations;
    const activeId = value.active_conversation_id;
    const activeConversation = value.active_conversation;
    const conversationIds = Array.isArray(conversations)
      ? conversations.map((item) => isRecord(item) ? String(item.id ?? "").trim() : "")
      : [];
    valid = Array.isArray(conversations)
      && conversations.length <= MAX_CONVERSATION_ITEMS
      && conversations.every(isConversationSummaryPayload)
      && conversationIds.every(Boolean)
      && new Set(conversationIds).size === conversationIds.length
      && (!("inventory_instance_id" in value)
        || value.inventory_instance_id === undefined
        || isBoundedString(value.inventory_instance_id, 128))
      && (!("inventory_revision" in value)
        || value.inventory_revision === undefined
        || isNonNegativeSafeInteger(value.inventory_revision))
      && (
        (("inventory_instance_id" in value) && value.inventory_instance_id !== undefined)
        === (("inventory_revision" in value) && value.inventory_revision !== undefined)
      )
      && (!('conversation_id' in value) || value.conversation_id === null || isBoundedString(value.conversation_id, 1_024))
      && (activeId === null || activeId === undefined || isBoundedString(activeId, 1_024))
      && (activeConversation === null || activeConversation === undefined || isConversationRecordPayload(activeConversation))
      && (!activeId || conversationIds.includes(String(activeId)))
      && (!activeConversation || !activeId || String(activeConversation.id) === String(activeId))
      && (!('session' in value) || value.session === null || value.session === undefined || isRuntimeSessionSnapshot(value.session))
      && isOptionalIsoTimestamp(value, "snapshot_at");
  }
  if (type === "conversation.switched") {
    const switchedId = value.conversation_id;
    const conversation = value.conversation;
    const conversationId = isRecord(conversation) ? String(conversation.id ?? "").trim() : "";
    valid = (
      switchedId === null
      || switchedId === undefined
      || isBoundedString(switchedId, 1_024)
    )
      && (conversation === null || conversation === undefined || isConversationRecordPayload(conversation))
      && (Boolean(switchedId) || Boolean(conversationId))
      && (!switchedId || !conversationId || String(switchedId) === conversationId)
      && (!('is_hydrating' in value) || typeof value.is_hydrating === "boolean")
      && (!('session' in value) || value.session === null || value.session === undefined || isRuntimeSessionSnapshot(value.session))
      && isOptionalIsoTimestamp(value, "snapshot_at");
  }
  if (type === "session.restored" || type === "session.synced") {
    const activeId = value.active_conversation_id;
    const conversation = value.conversation;
    const activeConversation = value.active_conversation;
    const session = value.session;
    const conversationId = isRecord(conversation) ? String(conversation.id ?? "").trim() : "";
    const activeConversationId = isRecord(activeConversation) ? String(activeConversation.id ?? "").trim() : "";
    valid = (!('session_id' in value) || isBoundedString(value.session_id, 1_024))
      && (!('restored' in value) || typeof value.restored === "boolean")
      && (!('synced' in value) || typeof value.synced === "boolean")
      && (activeId === null || activeId === undefined || isBoundedString(activeId, 1_024))
      && (conversation === null || conversation === undefined || isConversationRecordPayload(conversation))
      && (activeConversation === null || activeConversation === undefined || isConversationRecordPayload(activeConversation))
      && (!conversationId || !activeId || conversationId === String(activeId))
      && (!activeConversationId || !activeId || activeConversationId === String(activeId))
      && (!('conversation_switched_follows' in value) || typeof value.conversation_switched_follows === "boolean")
      && (!('workspace' in value) || isSessionWorkspacePayload(value.workspace))
      && isOptionalBoundedText(value, "working_directory", 32_768)
      && isOptionalBoundedText(value, "workspace_root", 32_768)
      && isOptionalBoundedText(value, "model", 4_096)
      && isOptionalBoundedText(value, "current_model", 4_096)
      && isOptionalBoundedText(value, "provider", 256)
      && isOptionalBoundedText(value, "provider_id", 1_024)
      && isOptionalBoundedText(value, "base_url", 4_096)
      && isOptionalBoundedText(value, "wire_api", 256)
      && (!('available_models' in value) || (
        Array.isArray(value.available_models)
        && value.available_models.length <= 4_096
        && value.available_models.every((model) => isBoundedString(model, 4_096))
      ))
      && (!('models_source' in value) || isBoundedString(value.models_source, 256, { allowEmpty: true }))
      && (!('messages' in value) || isBoundedRecordArray(value.messages, MAX_TRANSCRIPT_MESSAGES, {
        maxNodes: 16_384,
        maxDepth: 18,
        maxStringCharacters: 4_194_304,
        maxArrayItems: 8_192,
        maxObjectItems: 4_096,
      }))
      && (!('error' in value) || value.error === null || isBoundedString(value.error, MAX_EVENT_SUMMARY_CHARS, { allowEmpty: true }))
      && (!('missed_events' in value) || typeof value.missed_events === "boolean")
      && (!('event_log_gap' in value) || typeof value.event_log_gap === "boolean")
      && (!('snapshot_required' in value) || typeof value.snapshot_required === "boolean")
      && (!('cursor_reset' in value) || typeof value.cursor_reset === "boolean")
      && isOptionalIsoTimestamp(value, "snapshot_at")
      && (!('last_seq' in value) || isNonNegativeSafeInteger(value.last_seq))
      && (!('current_seq' in value) || isNonNegativeSafeInteger(value.current_seq))
      && (!('last_seq' in value) || !('current_seq' in value) || Number(value.current_seq) >= Number(value.last_seq))
      && (!('replayed_events' in value) || isNonNegativeSafeInteger(value.replayed_events))
      && (!('requested_last_seq' in value) || isNonNegativeSafeInteger(value.requested_last_seq))
      && (value.event_log_gap !== true || value.snapshot_required === true)
      && (
        value.cursor_reset !== true
        || (
          isNonNegativeSafeInteger(value.last_seq)
          && isNonNegativeSafeInteger(value.current_seq)
          && isNonNegativeSafeInteger(value.requested_last_seq)
          && Number(value.last_seq) === Number(value.current_seq)
          && Number(value.requested_last_seq) > Number(value.current_seq)
          && value.snapshot_required === true
          && Number(value.replayed_events ?? 0) === 0
        )
      )
      && (
        value.cursor_reset === true
        || !('requested_last_seq' in value)
        || !('last_seq' in value)
        || Number(value.requested_last_seq) === Number(value.last_seq)
      )
      && (!session || isRuntimeSessionSnapshot(session));
  }
  if (type === "session.replay") {
    const events = value.events;
    const nestedRecords = Array.isArray(events) && events.every(isRecord)
      ? events as Array<Record<string, unknown>>
      : [];
    const lastSeq = Number(value.last_seq);
    const currentSeq = Number(value.current_seq);
    valid = Array.isArray(events)
      && events.length <= MAX_SESSION_REPLAY_EVENTS
      && nestedRecords.length === events.length
      && isNonNegativeSafeInteger(value.last_seq)
      && isNonNegativeSafeInteger(value.current_seq)
      && currentSeq >= lastSeq
      && isNonNegativeSafeInteger(value.replayed_events)
      && Number(value.replayed_events) === events.length
      && events.every((item) => isRecord(item)
        && typeof item.type === "string"
        && SERVER_EVENT_TYPES.has(item.type as ServerEventType)
        && item.type !== "session.replay"
        && !String(item.type).startsWith("session.")
        && ![
          "artifact_content",
          "conversation.list",
          "conversation.switched",
          "llm.model.updated",
          "mcp_status",
          "pong",
          "runtime.capabilities",
          "stream_resume",
        ].includes(String(item.type)))
      && nestedRecords.every((item, index) => {
        if (!isNonNegativeSafeInteger(item.seq) || !isNonNegativeSafeInteger(item.previous_replay_seq)) {
          return false;
        }
        const seq = Number(item.seq);
        const previousReplaySeq = Number(item.previous_replay_seq);
        const expectedPrevious = index === 0
          ? lastSeq
          : Number(nestedRecords[index - 1].seq);
        return previousReplaySeq === expectedPrevious
          && seq > previousReplaySeq
          && seq <= currentSeq;
      })
      && (nestedRecords.length > 0
        ? Number(nestedRecords[nestedRecords.length - 1].seq) === currentSeq
        : lastSeq === currentSeq);
  }
  if (type === "context_usage") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isNonNegativeSafeInteger(value.used)
      && isNonNegativeSafeInteger(value.limit)
      && (!("ledger" in value) || isContextLedgerPayload(value.ledger));
  }
  if (type === "context_compacted") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.summary, MAX_JSON_STRING_CHARS)
      && (!("before_tokens" in value) || isNonNegativeSafeInteger(value.before_tokens))
      && (!("after_tokens" in value) || isNonNegativeSafeInteger(value.after_tokens))
      && (!("retained_categories" in value) || (
        Array.isArray(value.retained_categories)
        && value.retained_categories.length <= 64
        && value.retained_categories.every((category) => isBoundedString(category, 256))
        && new Set(value.retained_categories).size === value.retained_categories.length
      ))
      && (!("ledger" in value) || isContextLedgerPayload(value.ledger));
  }
  if (type === "budget_update") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isNonNegativeSafeInteger(value.used)
      && isNonNegativeSafeInteger(value.total)
      && isNonNegativeNumberRecord(value.breakdown);
  }
  if (type === "budget.warning") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.bucket, 256)
      && typeof value.percent === "number"
      && Number.isFinite(value.percent)
      && value.percent >= 0
      && value.percent <= 1
      && typeof value.will_compact === "boolean";
  }
  if (type === "stream_event") {
    valid = isNonEmptyString(value.provider)
      && isNonEmptyString(value.event_type)
      && isRecord(value.data)
      && hasOptionalKind(value, "sdk_only", "boolean");
  }
  if (type === "rate_limit") {
    valid = isNonEmptyString(value.error_type)
      && hasOptionalKind(value, "provider", "string")
      && hasOptionalKind(value, "message", "string")
      && hasOptionalKind(value, "recoverable", "boolean")
      && hasOptionalKind(value, "retry_after_seconds", "number")
      && hasOptionalKind(value, "retry_at", "number")
      && (!("retry_after_seconds" in value) || Number(value.retry_after_seconds) >= 0)
      && (!("retry_at" in value) || Number(value.retry_at) >= 0);
  }
  if (type === "session.state_changed") {
    valid = (value.state === "idle" || value.state === "working")
      && hasOptionalKind(value, "run_id", "string")
      && hasOptionalKind(value, "reason", "string");
  }
  if (type === "command_output_chunk") {
    const id = "id" in value ? value.id : undefined;
    const toolCallId = "tool_call_id" in value ? value.tool_call_id : undefined;
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && isBoundedString(value.content, MAX_COMMAND_OUTPUT_CHARS, { allowEmpty: true })
      && (value.stream === "stdout" || value.stream === "stderr")
      && (id === undefined || isBoundedString(id, 1_024))
      && (toolCallId === undefined || isBoundedString(toolCallId, 1_024))
      && (id === undefined || toolCallId === undefined || id === toolCallId)
      && (!("turn_id" in value) || isBoundedString(value.turn_id, 1_024));
  }
  if (type === "image_chunk") {
    const mediaType = String(value.media_type || "").trim().toLowerCase();
    const hasLiveImage = "image_data" in value;
    const hasOmittedImage = value.image_data_omitted === true;
    valid = RASTER_IMAGE_MEDIA_TYPES.has(mediaType)
      && (hasLiveImage !== hasOmittedImage)
      && (
        hasLiveImage
          ? isValidBase64Payload(value.image_data)
            && hasMatchingRasterImageMagic(String(value.image_data), mediaType)
            && (!("image_data_size" in value) || value.image_data_size === String(value.image_data).length)
            && !("image_data_omitted" in value)
          : isNonNegativeSafeInteger(value.image_data_size)
            && Number(value.image_data_size) <= MAX_GENERATED_IMAGE_BASE64_CHARS
            && !("image_data" in value)
      );
  }
  if (type === "parent.notifications") {
    valid = Number.isSafeInteger(value.count)
      && Number(value.count) > 0
      && isBoundedString(value.parent_run_id, 1_024);
  }
  if (type === "subagent.plan_approval_requested") {
    // The prompt is unanswerable without its owner and routing ids, and the
    // plan body is user-facing markdown, so it is bounded like any other large
    // event content instead of being trusted at arbitrary length.
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.subagent_id, 1_024)
      && isBoundedString(value.request_id, 1_024)
      && isOptionalBoundedText(value, "plan_content", MAX_EVENT_CONTENT_CHARS)
      && [
        ["teammate_name", 1_024],
        ["team_name", 1_024],
        ["plan_file_path", 32_768],
      ].every(([field, maximum]) => isOptionalBoundedText(
        value,
        field as string,
        maximum as number,
      ));
  }
  if (type === "commands.list") {
    const conversationOwnerPresent = Object.prototype.hasOwnProperty.call(value, "conversation_id");
    valid = conversationOwnerPresent
      && (value.conversation_id === null || isBoundedString(value.conversation_id, 1_024))
      && Array.isArray(value.commands)
      && value.commands.length <= 2_048
      && value.commands.every(isCommandCatalogEntry)
      && (!("request_id" in value) || isBoundedString(value.request_id, 1_024));
  }
  if (type === "pong") {
    const envelopeFields = new Set([
      "type",
      "seq",
      "event_id",
      "timestamp",
      "task_id",
      "turn_id",
      "client_command_id",
      "client_command_type",
    ]);
    valid = Object.keys(value).every((field) => envelopeFields.has(field));
  }
  if (type === "system_notice") {
    const hasContent = isBoundedString(value.content, MAX_NOTICE_TEXT_CHARS);
    const hasTitledMessage = isBoundedString(value.title, 1_024)
      && isBoundedString(value.message, MAX_NOTICE_TEXT_CHARS);
    valid = (hasContent || hasTitledMessage)
      && (!("content" in value) || isBoundedString(value.content, MAX_NOTICE_TEXT_CHARS))
      && (!("title" in value) || isBoundedString(value.title, 1_024))
      && (!("message" in value) || isBoundedString(value.message, MAX_NOTICE_TEXT_CHARS))
      && (!("data" in value) || isRecord(value.data))
      && (!("checkpoint_origin" in value) || isCheckpointOrigin(value.checkpoint_origin));
  }
  if (type === "workspace.imported") {
    const project = value.project;
    valid = isWorkspaceProject(project)
      && isBoundedString(value.summary, 65_536, { allowEmpty: true })
      && isNonNegativeSafeInteger(value.file_count)
      && value.file_count === project.file_count
      && normalizeWorkspaceRoot(value.workspace_root) === normalizeWorkspaceRoot(project.root_path)
      && (!("request_id" in value) || isBoundedString(value.request_id, 1_024));
  }
  if (type === "approval.file_diff") {
    valid = isNonEmptyString(value.tool_call_id)
      && isNonEmptyString(value.path)
      && typeof value.patch === "string"
      && typeof value.is_large === "boolean"
      && typeof value.is_truncated === "boolean";
  }
  if (type === "context_forked") {
    const branchCreated = value.branch_created === true;
    const branchActivated = value.branch_activated === true;
    valid = isNonEmptyString(value.fork_id)
      && isNonEmptyString(value.parent_conversation_id)
      && isNonNegativeSafeInteger(value.message_index)
      && isNonNegativeSafeInteger(value.context_history_index)
      && isNonNegativeSafeInteger(value.history_length)
      && isNonNegativeSafeInteger(value.estimated_tokens)
      && typeof value.branch_created === "boolean"
      && typeof value.branch_activated === "boolean"
      && (!branchCreated || isNonEmptyString(value.branch_conversation_id))
      && (!branchActivated || (
        branchCreated
        && value.conversation_id === value.branch_conversation_id
      ))
      && (!("message_id" in value) || isNonEmptyString(value.message_id))
      && (!("created_at" in value) || (
        isNonEmptyString(value.created_at)
        && Number.isFinite(Date.parse(value.created_at))
      ))
      && (!("status" in value) || isNonEmptyString(value.status));
  }
  if (type === "context_ledger") {
    valid = value.schema_version === 1
      && isNonNegativeSafeInteger(value.estimated_tokens)
      && isNonNegativeSafeInteger(value.actual_tokens)
      && isNonNegativeSafeInteger(value.compaction_count)
      && isNonNegativeSafeInteger(value.native_attachment_tokens)
      && isNonNegativeSafeInteger(value.native_attachment_count)
      && Array.isArray(value.entries)
      && value.entries.every(isContextLedgerEntry);
  }
  if (type === "context_side_query_result") {
    valid = isNonEmptyString(value.query)
      && typeof value.result === "string"
      && typeof value.focus === "string";
  }
  if (type === "control_request") valid = hasValidControlRequest(value);
  if (type === "llm.provider.oauth.auth") {
    valid = isNonEmptyString(value.provider)
      && isSafeHttpUrl(value.url)
      && (!("instructions" in value) || isNonEmptyString(value.instructions));
  }
  if (type === "llm.provider.oauth.device_code") {
    valid = isNonEmptyString(value.provider)
      && isNonEmptyString(value.userCode)
      && isSafeHttpUrl(value.verificationUri)
      && (!("intervalSeconds" in value) || isPositiveFiniteNumber(value.intervalSeconds))
      && (!("expiresInSeconds" in value) || isPositiveFiniteNumber(value.expiresInSeconds));
  }
  if (type === "llm.provider.oauth.info") {
    valid = isNonEmptyString(value.provider)
      && isNonEmptyString(value.message)
      && (!("links" in value) || (
        Array.isArray(value.links)
        && value.links.length <= 16
        && value.links.every(isProviderOAuthLink)
      ));
  }
  if (type === "llm.provider.oauth.progress") {
    valid = isNonEmptyString(value.provider) && isNonEmptyString(value.message);
  }
  if (type === "conversation.compaction.updated") {
    valid = value.state === "compacted" && isNonEmptyString(value.summary);
  }
  if (type === "conversation.summary.updated") {
    valid = typeof value.summary === "string"
      && isNonEmptyString(value.title)
      && isNonEmptyString(value.updated_at)
      && Number.isFinite(Date.parse(value.updated_at))
      && (value.memory_mode === "enabled" || value.memory_mode === "disabled" || value.memory_mode === "polluted")
      && typeof value.memory_polluted === "boolean"
      && hasOnlyNonEmptyStrings(value.memory_pollution_sources);
  }
  if (type === "checkpoint.created") {
    valid = isCheckpointRecord(value, value.conversation_id, value.workspace_root);
  }
  if (type === "checkpoint.list") {
    valid = Array.isArray(value.checkpoints)
      && value.checkpoints.length <= 4_096
      && value.checkpoints.every((checkpoint) => isCheckpointRecord(
        checkpoint,
        value.conversation_id,
        value.workspace_root,
      ));
  }
  if (type === "checkpoint.rewound") {
    valid = isCheckpointRecord(
      value.checkpoint,
      value.conversation_id,
      value.workspace_root,
    );
  }
  if (type === "checkpoint.run.list") {
    valid = Array.isArray(value.checkpoints)
      && value.checkpoints.every(isRecord)
      && Array.isArray(value.runs)
      && value.runs.every(isRecord)
      && Array.isArray(value.subagents)
      && value.subagents.every(isRecord);
  }
  if (type === "checkpoint.run.resume") {
    valid = value.resumed === true
      ? typeof value.session_id === "string"
        && typeof value.run_id === "string"
        && typeof value.iteration === "number"
        && Number.isFinite(value.iteration)
      : typeof value.message === "string";
  }
  if (type === "workspace.recent.list") {
    valid = Array.isArray(value.projects) && value.projects.every(isRecentWorkspace);
  }
  if (type === "permission.rules.updated") valid = hasValidPermissionRules(value.rules);
  if (type === "guidelines.updated") {
    valid = (!("path" in value) || typeof value.path === "string")
      && (!("cache_cleared" in value) || typeof value.cache_cleared === "boolean")
      && (!("effective_from" in value) || typeof value.effective_from === "string")
      && (!("source_kind" in value) || typeof value.source_kind === "string")
      && (!("parent_path" in value) || typeof value.parent_path === "string");
  }
  if (type === "preview.refreshed") {
    valid = isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.workspace_root, 32_768)
      && (!("request_id" in value) || isBoundedString(value.request_id, 1_024))
      && (!("url" in value) || isSafeHttpUrl(value.url))
      && (!("path" in value) || isWorkspaceRelativePath(value.path));
  }
  if (type === "terminal.output") {
    const streamShape = isBoundedString(value.session_id, 1_024)
      && typeof value.data === "string"
      && value.data.length > 0
      && value.data.length <= MAX_TERMINAL_OUTPUT_CHARS
      && !("command" in value)
      && !("output" in value)
      && !("exit_code" in value);
    const commandShape = isBoundedString(value.command, 4_096)
      && isBoundedString(value.output, MAX_TERMINAL_OUTPUT_CHARS, { allowEmpty: true })
      && !("session_id" in value)
      && !("data" in value)
      && (!("exit_code" in value) || Number.isSafeInteger(value.exit_code));
    valid = isBoundedString(value.conversation_id, 1_024)
      && (streamShape !== commandShape);
  }
  if (type === "user_message.queue.updated") {
    const queued = value.status === "queued";
    const dequeued = value.status === "dequeued";
    const cancelled = value.status === "cancelled";
    const turnMode = value.turn_mode;
    valid = (queued || dequeued || cancelled)
      && isBoundedString(value.conversation_id, 1_024)
      && isBoundedString(value.message_id, 1_024)
      && (!("user_message_id" in value) || isBoundedString(value.user_message_id, 1_024))
      && (!("reason" in value) || isBoundedString(value.reason, 16_384))
      && (!("target_message_id" in value) || isBoundedString(value.target_message_id, 1_024))
      && (turnMode === undefined || turnMode === "follow_up" || turnMode === "steer")
      && (queued
        ? Number.isSafeInteger(value.position) && Number(value.position) > 0
        : !("position" in value))
      && (turnMode === undefined || dequeued)
      && (turnMode !== "steer" || value.reason === "steered_current_turn")
      && (value.reason !== "steered_current_turn" || dequeued);
  }
  if (!valid) {
    // Name the shape, not the content: a payload can carry user text, but the
    // key set is what tells a protocol mismatch apart from a missing field.
    console.warn(
      "[ws] Dropping server event with invalid semantic payload",
      type,
      Object.keys(value).sort().join(","),
    );
  }
  return valid;
};

/** True when the wire event names a type this client build does not declare.
 *
 * A registry gap is protocol drift, not stream desync. The two need different
 * remedies: a malformed or out-of-order event is repaired by resyncing, but a
 * type the client cannot represent is replayed from the durable log on every
 * resync, so resyncing on it loops forever. The socket layer consults this to
 * tell the two apart. */
export const isUnknownServerEventType = (value: unknown): boolean => {
  if (!isRecord(value)) return false;
  const type = value.type;
  if (typeof type !== "string" || !type) return false;
  return !SERVER_EVENT_TYPES.has(type as ServerEventType);
};

export const normalizeInboundServerEvent = (value: unknown): ServerEvent | null => {
  if (!isRecord(value)) {
    console.warn("[ws] Dropping non-object server event", value);
    return null;
  }

  const type = value.type;
  if (typeof type !== "string" || !type) {
    console.warn("[ws] Dropping server event without a string type", value);
    return null;
  }

  if (!SERVER_EVENT_TYPES.has(type as ServerEventType)) {
    console.warn("[ws] Dropping unknown server event type", type);
    return null;
  }

  const knownType = type as ServerEventType;
  if (!hasValidEnvelope(value, type) || !hasRequiredRoutingFields(value, knownType)) return null;
  if (CONVERSATION_OWNED_EVENT_TYPES.has(knownType) && !isNonEmptyString(value.conversation_id)) {
    console.warn("[ws] Dropping server event without conversation owner", type);
    return null;
  }
  if (WORKSPACE_OWNED_EVENT_TYPES.has(knownType) && !isNonEmptyString(value.workspace_root)) {
    console.warn("[ws] Dropping server event without workspace owner", type);
    return null;
  }
  if (!hasValidSemanticPayload(value, knownType)) return null;

  return value as ServerEvent;
};
