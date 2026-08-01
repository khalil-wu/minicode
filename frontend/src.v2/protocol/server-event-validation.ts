import { SERVER_EVENT_TYPES, type ServerEvent, type ServerEventType } from "./events";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

type FieldKind = "array" | "boolean" | "number" | "record" | "string";

const REQUIRED_ROUTING_FIELDS: Partial<
  Record<ServerEventType, Readonly<Record<string, FieldKind>>>
> = {
  "agent.run.started": { run_id: "string", status: "string" },
  "agent.run.completed": { run_id: "string", status: "string" },
  "approval_request": { tool_call_id: "string", tool_name: "string", args: "record" },
  "permission.decision": { tool_call_id: "string", tool_name: "string", decision: "string" },
  "tool_call": { id: "string", name: "string", args: "record" },
  "tool_output_delta": { id: "string", output: "string" },
  "tool_result": { id: "string", summary: "string" },
  "client.command.ack": { client_command_id: "string", command_type: "string" },
  "session.replay": { events: "array" },
  "subagent.start": { subagent_id: "string" },
  "subagent.progress": { subagent_id: "string" },
  "subagent.done": { subagent_id: "string" },
};

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
  for (const field of ["event_id", "timestamp", "task_id", "turn_id"] as const) {
    if (field in value && typeof value[field] !== "string") {
      console.warn(`[ws] Dropping server event with invalid ${field}`, type, value[field]);
      return false;
    }
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

  return value as ServerEvent;
};
