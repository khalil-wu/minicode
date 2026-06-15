import { SERVER_EVENT_TYPES, type ServerEvent, type ServerEventType } from "./events";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

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
    console.warn("[ws] Unknown server event type", type);
  }

  if ("seq" in value && typeof value.seq !== "number") {
    console.warn("[ws] Server event seq should be a number", type, value.seq);
  }

  if ("event_id" in value && typeof value.event_id !== "string") {
    console.warn("[ws] Server event event_id should be a string", type, value.event_id);
  }

  return value as ServerEvent;
};
