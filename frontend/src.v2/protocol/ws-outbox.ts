import type { ClientCommand, CommandResultEvent } from "./events";
import { pushToast } from "../overlays/ToastContainer";
import { isDesktop, ptyKillConversation } from "../desktop/runtime";

type Sender = (command: ClientCommand) => boolean;
type SendClientCommandOptions = { silent?: boolean };

let sender: Sender | null = null;
const pendingCommandResults = new Map<string, {
  expectedCommand: string;
  resolve: (event: CommandResultEvent) => void;
  reject: (error: Error) => void;
}>();

export const createClientCommandId = (): string => {
  const randomPart = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
  return `cmd_${randomPart}`;
};

export const commandWithClientCommandId = (command: ClientCommand): ClientCommand => {
  if (typeof command.client_command_id === "string" && command.client_command_id) {
    return command;
  }
  return { ...command, client_command_id: createClientCommandId() };
};

export const registerWebSocketSender = (nextSender: Sender | null) => {
  sender = nextSender;
};

const shouldNotifyOffline = (command: ClientCommand, options?: SendClientCommandOptions): boolean =>
  !options?.silent && !(command as ClientCommand & { silent?: boolean }).silent;

export const sendClientCommand = (command: ClientCommand, options?: SendClientCommandOptions): boolean => {
  if (!sender) {
    if (shouldNotifyOffline(command, options)) {
      pushToast("Operation failed: connection is offline.", "error", 3000);
    }
    return false;
  }
  const sent = sender(command);
  if (!sent && shouldNotifyOffline(command, options)) {
    pushToast("Operation failed: connection is offline.", "error", 3000);
  }
  return sent;
};

export const sendConversationDeleteCommand = async (
  command: Extract<ClientCommand, { type: "conversation.delete" }>,
): Promise<boolean> => {
  if (isDesktop()) {
    try {
      await ptyKillConversation(command.conversation_id);
    } catch (error) {
      pushToast(`Unable to stop the conversation terminal: ${String(error)}`, "error", 4000);
      return false;
    }
  }
  return sendClientCommand(command);
};

export const sendClientCommandAwaitResult = (
  command: ClientCommand,
  expectedCommand: string,
): Promise<CommandResultEvent> => {
  const commandWithId = commandWithClientCommandId(command);
  const clientCommandId = String(commandWithId.client_command_id || "");
  return new Promise<CommandResultEvent>((resolve, reject) => {
    pendingCommandResults.set(clientCommandId, { expectedCommand, resolve, reject });
    if (sendClientCommand(commandWithId)) return;
    pendingCommandResults.delete(clientCommandId);
    reject(new Error("Connection is offline"));
  });
};

export const resolveClientCommandResult = (event: CommandResultEvent): boolean => {
  const clientCommandId = typeof event.data?.client_command_id === "string"
    ? event.data.client_command_id
    : "";
  if (!clientCommandId) return false;
  const pending = pendingCommandResults.get(clientCommandId);
  if (!pending || pending.expectedCommand !== event.command) return false;
  pendingCommandResults.delete(clientCommandId);
  pending.resolve(event);
  return true;
};

export const rejectClientCommandResult = (
  clientCommandId: string,
  reason: string,
): boolean => {
  const pending = pendingCommandResults.get(clientCommandId);
  if (!pending) return false;
  pendingCommandResults.delete(clientCommandId);
  pending.reject(new Error(reason || "Command was rejected by the server"));
  return true;
};

export const resetPendingCommandResultsForTests = () => {
  for (const pending of pendingCommandResults.values()) {
    pending.reject(new Error("Pending command result reset"));
  }
  pendingCommandResults.clear();
};
