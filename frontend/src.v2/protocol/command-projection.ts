import type { ClientCommand, ServerEvent } from "./events";
import { workspacePathComparisonKey } from "../lib/workspace-path";

// A semantic projection can outlive transport ACK and its pending spinner.
// Keep the latest command receipt for each view it can update.
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

const skillsProjectionDomain = (conversationId: string | null | undefined, workspaceRoot: unknown): string =>
  `skills.list:${JSON.stringify([conversationId || "", workspacePathComparisonKey(workspaceRoot)])}`;

const projectionDomainsForCommand = (command: ClientCommand, activeConversationId?: string | null, activeWorkspaceRoot?: string): string[] => {
  if (command.type === "skills.list") return [skillsProjectionDomain(command.conversation_id || activeConversationId, command.workspace_root || activeWorkspaceRoot)];
  if (command.type === "conversation.create" && (command.activate === false || command.conversation_type === "side_chat")) return [];
  if (command.type === "context.fork" && (!command.create_branch || !command.activate)) return command.create_branch ? ["conversation-list"] : [];
  if (command.type === "conversation.archive") return command.archive_navigation === "home"
    ? command.conversation_id === activeConversationId ? ["active-conversation", "conversation-list"] : ["conversation-list"] : [];
  if (command.type === "conversation.delete" && command.conversation_id !== activeConversationId) return [];
  if (command.type === "user_message" && (command.conversation_id || activeConversationId)) return [];
  return Array.from(new Set(Object.values(COMMAND_PROJECTION_DOMAINS[command.type] ?? {}).flat()));
};

export const rememberSentCommandProjection = (command: ClientCommand, clientCommandId: string, activeConversationId?: string | null, activeWorkspaceRoot?: string): void => {
  const domains = projectionDomainsForCommand(command, activeConversationId, activeWorkspaceRoot);
  for (const domain of domains) latestProjectionCommandIds.set(domain, clientCommandId);
  if (domains.includes("active-conversation") && command.type !== "session.restore" && command.type !== "session.sync") {
    latestProjectionCommandIds.set("foreground-intent", clientCommandId);
  }
};

export const rememberLocalConversationDraftSelection = (draftId: string): void => {
  latestProjectionCommandIds.set("active-conversation", draftId);
  latestProjectionCommandIds.set("foreground-intent", draftId);
};

export const foregroundNavigationIntent = () => latestProjectionCommandIds.get("foreground-intent");

// These payloads still carry canonical owner data or complete recovery when
// their activation has been superseded by later local navigation.
export const carriesConversationSnapshot = (event: ServerEvent): boolean =>
  event.type === "conversation.switched" || event.type === "session.restored" || event.type === "session.synced";

export const isRequestedForegroundNavigation = (event: ServerEvent): boolean =>
  Boolean(event.client_command_id && event.client_command_type
    && event.client_command_type !== "session.restore" && event.client_command_type !== "session.sync"
    && COMMAND_PROJECTION_DOMAINS[event.client_command_type]?.[event.type]?.includes("active-conversation")
    && latestProjectionCommandIds.get("foreground-intent") === event.client_command_id);

export const isSupersededPendingProjectionCommand = (command: ClientCommand, activeConversationId?: string | null): boolean => {
  if (!["conversation.list", "conversation.switch", "session.restore", "session.sync"].includes(command.type)) return false;
  const id = String(command.client_command_id || "");
  return Boolean(id) && projectionDomainsForCommand(command, activeConversationId).some(domain => {
    if (domain === "active-conversation" && command.type !== "session.restore" && command.type !== "session.sync"
      && latestProjectionCommandIds.get("foreground-intent") === id) return false;
    const latest = latestProjectionCommandIds.get(domain);
    return Boolean(latest && latest !== id);
  });
};

export const isSupersededCommandProjection = (event: ServerEvent): boolean => {
  if (!event.client_command_id || !event.client_command_type) return false;
  if (event.client_command_type === "conversation.archive" && (event as unknown as { archive_navigation?: string }).archive_navigation !== "home") return false;
  const domains = event.type === "skills.list" && event.client_command_type === "skills.list"
    ? [skillsProjectionDomain(event.conversation_id, event.workspace_root)]
    : COMMAND_PROJECTION_DOMAINS[event.client_command_type]?.[event.type] ?? [];
  return domains.some(domain => {
    if (domain === "active-conversation" && event.client_command_type !== "session.restore" && event.client_command_type !== "session.sync"
      && latestProjectionCommandIds.get("foreground-intent") === event.client_command_id) return false;
    const latest = latestProjectionCommandIds.get(domain);
    return Boolean(latest && latest !== event.client_command_id);
  });
};
