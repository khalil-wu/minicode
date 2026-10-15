import type { ClientCommand, ServerEvent } from "./events";

// A semantic projection can outlive transport ACK and its pending spinner.
// Keep the latest command receipt for each view it can activate.
export const latestProjectionCommandIds = new Map<string, string>();
const COMMAND_PROJECTION_DOMAINS: Readonly<Record<string, Readonly<Record<string, readonly string[]>>>> = {
  "conversation.list": { "conversation.list": ["conversation-list"] },
  "conversation.switch": { "conversation.switched": ["active-conversation"], "conversation.list": ["conversation-list"] },
  "conversation.create": { "conversation.switched": ["active-conversation"] },
  "context.fork": { "conversation.switched": ["active-conversation"], "conversation.list": ["conversation-list"] },
  "conversation.archive": { "conversation.switched": ["active-conversation"], "conversation.list": ["conversation-list"] },
  "workspace.set": { "conversation.switched": ["active-conversation"] },
  "conversation.delete": { "conversation.switched": ["active-conversation"] },
  "user_message": { "conversation.switched": ["active-conversation"] },
  "session.restore": { "session.restored": ["session-state", "active-conversation"], "conversation.switched": ["active-conversation"], "session.replay": ["session-recovery"], "stream_resume": ["session-recovery"] },
  "session.sync": { "session.synced": ["session-state", "active-conversation"], "session.replay": ["session-recovery"], "stream_resume": ["session-recovery"] },
};

const projectionDomainsForCommand = (command: ClientCommand, activeConversationId?: string | null): string[] => {
  if (command.type === "conversation.create" && (command.activate === false || command.conversation_type === "side_chat")) return [];
  if (command.type === "context.fork" && (!command.create_branch || !command.activate)) return command.create_branch ? ["conversation-list"] : [];
  if (command.type === "conversation.archive") return command.archive_navigation === "home"
    ? command.conversation_id === activeConversationId ? ["active-conversation", "conversation-list"] : ["conversation-list"] : [];
  if (command.type === "conversation.delete" && command.conversation_id !== activeConversationId) return [];
  if (command.type === "user_message" && (command.conversation_id || activeConversationId)) return [];
  return Array.from(new Set(Object.values(COMMAND_PROJECTION_DOMAINS[command.type] ?? {}).flat()));
};

export const rememberSentCommandProjection = (command: ClientCommand, clientCommandId: string, activeConversationId?: string | null): void => {
  for (const domain of projectionDomainsForCommand(command, activeConversationId)) latestProjectionCommandIds.set(domain, clientCommandId);
};

export const isSupersededPendingProjectionCommand = (command: ClientCommand, activeConversationId?: string | null): boolean => {
  if (!["conversation.list", "conversation.switch", "session.restore", "session.sync"].includes(command.type)) return false;
  const id = String(command.client_command_id || "");
  return Boolean(id) && projectionDomainsForCommand(command, activeConversationId).some(domain => {
    const latest = latestProjectionCommandIds.get(domain);
    return Boolean(latest && latest !== id);
  });
};

export const isSupersededCommandProjection = (event: ServerEvent): boolean => {
  if (!event.client_command_id || !event.client_command_type) return false;
  if (event.client_command_type === "conversation.archive" && (event as unknown as { archive_navigation?: string }).archive_navigation !== "home") return false;
  const domains = COMMAND_PROJECTION_DOMAINS[event.client_command_type]?.[event.type] ?? [];
  return domains.some(domain => {
    const latest = latestProjectionCommandIds.get(domain);
    return Boolean(latest && latest !== event.client_command_id);
  });
};
