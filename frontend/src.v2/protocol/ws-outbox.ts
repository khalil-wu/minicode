import type { ClientCommand, CommandResultEvent } from "./events";
import { pushToast } from "../overlays/ToastContainer";
import { isDesktop } from "../desktop/runtime";

type Sender = (command: ClientCommand) => boolean;
type SendClientCommandOptions = { silent?: boolean };
export type AwaitCommandResultOptions = {
  timeoutMs?: number;
  silent?: boolean;
};

export const DEFAULT_COMMAND_RESULT_TIMEOUT_MS = 60_000;
export const LONG_COMMAND_RESULT_TIMEOUT_MS = 10 * 60_000;

let sender: Sender | null = null;
let senderFailureReason: (() => string) | null = null;
const pendingCommandResults = new Map<string, {
  expectedCommand: string;
  resolve: (event: CommandResultEvent) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}>();

export const createClientCommandId = (): string => {
  const randomPart = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID().replace(/-/g, "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
  return `cmd_${randomPart}`;
};

export const commandWithClientCommandId = (command: ClientCommand): ClientCommand => {
  if (typeof command.client_command_id === "string" && command.client_command_id) {
    return command;
  }
  return { ...command, client_command_id: createClientCommandId() };
};

export const registerWebSocketSender = (nextSender: Sender | null, failureReason?: () => string) => {
  sender = nextSender;
  senderFailureReason = nextSender && failureReason ? failureReason : null;
};

const sendFailureMessage = (): string => senderFailureReason?.() || "连接已断开";

const shouldNotifyOffline = (command: ClientCommand, options?: SendClientCommandOptions): boolean =>
  !options?.silent && !(command as ClientCommand & { silent?: boolean }).silent;

export const sendClientCommand = (command: ClientCommand, options?: SendClientCommandOptions): boolean => {
  if (!sender) {
    if (shouldNotifyOffline(command, options)) {
      pushToast("操作失败：连接已断开。", "error", 3000);
    }
    return false;
  }
  const sent = sender(command);
  if (!sent && shouldNotifyOffline(command, options)) {
    pushToast(`操作失败：${sendFailureMessage()}。`, "error", 3000);
  }
  return sent;
};

export const sendConversationDeleteCommand = async (
  command: Extract<ClientCommand, { type: "conversation.delete" }>,
): Promise<boolean> => {
  const resultPromise = sendClientCommandAwaitResult(
    { ...command, client_resource_cleanup: isDesktop() },
    "conversation.delete",
    // Isolated-worktree deletion may include a recoverable snapshot and
    // several bounded git operations.  Give that authoritative backend fence
    // the long-operation budget instead of reporting a false failure at the
    // generic 60-second command timeout.
    { silent: true, timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
  );
  try {
    const result = await resultPromise;
    if (commandResultSucceeded(result)) return true;
    pushToast(result.message || "会话删除失败，请稍后重试。", "error", 6000);
    return false;
  } catch (error) {
    pushToast(
      error instanceof Error ? error.message : "会话删除失败，请检查连接后重试。",
      "error",
      6000,
    );
    return false;
  }
};

export const sendClientCommandAwaitResult = (
  command: ClientCommand,
  expectedCommand: string,
  options: AwaitCommandResultOptions = {},
): Promise<CommandResultEvent> => {
  const commandWithId = commandWithClientCommandId(command);
  const clientCommandId = String(commandWithId.client_command_id || "");
  const configuredTimeout = Number(options.timeoutMs ?? DEFAULT_COMMAND_RESULT_TIMEOUT_MS);
  const timeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
    ? configuredTimeout
    : DEFAULT_COMMAND_RESULT_TIMEOUT_MS;
  return new Promise<CommandResultEvent>((resolve, reject) => {
    const previous = pendingCommandResults.get(clientCommandId);
    if (previous) {
      clearTimeout(previous.timer);
      previous.reject(new Error(`命令 ${expectedCommand} 替换了先前的待处理请求`));
    }
    const timer = setTimeout(() => {
      const pending = pendingCommandResults.get(clientCommandId);
      if (!pending || pending.expectedCommand !== expectedCommand) return;
      pendingCommandResults.delete(clientCommandId);
      pending.reject(new Error(
        `操作超时：${expectedCommand} 在 ${Math.ceil(timeoutMs / 1000)} 秒内没有返回结果`,
      ));
    }, timeoutMs);
    pendingCommandResults.set(clientCommandId, {
      expectedCommand,
      resolve,
      reject,
      timer,
    });
    if (sendClientCommand(commandWithId, { silent: options.silent })) return;
    const pending = pendingCommandResults.get(clientCommandId);
    if (pending) clearTimeout(pending.timer);
    pendingCommandResults.delete(clientCommandId);
    reject(new Error(sendFailureMessage()));
  });
};

/**
 * Wait for the backend's semantic acceptance of a blocking-prompt response.
 * A transport ACK only admits the command durably; it neither settles the
 * prompt nor proves the tool ran. All prompt responses use the existing
 * command-result registry, including control responses and cancellations.
 */
export const sendPromptResponseCommand = async (
  command: ClientCommand,
): Promise<CommandResultEvent> => {
  return sendClientCommandAwaitResult(command, command.type);
};

/** A lost connection leaves control decisions unconfirmed, not completed.
 * Return their ids so the transport withdraws them instead of silently
 * replaying a decision after its caller has been offered an explicit retry. */
export const rejectPendingPromptResponseResults = (reason: string): string[] => {
  const ids = Array.from(pendingCommandResults.entries())
    .filter(([, pending]) => pending.expectedCommand === "control_response"
      || pending.expectedCommand === "control_cancel_request")
    .map(([id]) => id);
  for (const id of ids) rejectClientCommandResult(id, reason);
  return ids;
};

export const commandResultSucceeded = (event: CommandResultEvent): boolean => {
  const level = String(event.level || "").toLowerCase();
  return level !== "error" && level !== "failed";
};

export const resolveClientCommandResult = (event: CommandResultEvent & { client_command_id?: string }): boolean => {
  const clientCommandId = event.client_command_id || (typeof event.data?.client_command_id === "string"
    ? event.data.client_command_id
    : "");
  if (!clientCommandId) return false;
  const pending = pendingCommandResults.get(clientCommandId);
  if (!pending || pending.expectedCommand !== event.command) return false;
  pendingCommandResults.delete(clientCommandId);
  clearTimeout(pending.timer);
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
  clearTimeout(pending.timer);
  pending.reject(new Error(reason || "Command was rejected by the server"));
  return true;
};

export const rejectAllPendingCommandResults = (reason: string): number => {
  const pendingEntries = Array.from(pendingCommandResults.values());
  pendingCommandResults.clear();
  for (const pending of pendingEntries) {
    clearTimeout(pending.timer);
    pending.reject(new Error(reason || "Connection closed before the operation completed"));
  }
  return pendingEntries.length;
};

export const resetPendingCommandResultsForTests = () => {
  rejectAllPendingCommandResults("Pending command result reset");
};
