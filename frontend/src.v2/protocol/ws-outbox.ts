import type { ClientCommand, CommandResultEvent } from "./events";
import { pushToast } from "../overlays/ToastContainer";
import { embeddedBrowserCloseConversation, isDesktop, ptyKillConversation } from "../desktop/runtime";

type Sender = (command: ClientCommand) => boolean;
type SendClientCommandOptions = { silent?: boolean };
export type AwaitCommandResultOptions = {
  timeoutMs?: number;
  silent?: boolean;
};

export const DEFAULT_COMMAND_RESULT_TIMEOUT_MS = 60_000;
export const LONG_COMMAND_RESULT_TIMEOUT_MS = 10 * 60_000;
const LOCAL_CONVERSATION_CLEANUP_TIMEOUT_MS = 2_500;

let sender: Sender | null = null;
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

export const registerWebSocketSender = (nextSender: Sender | null) => {
  sender = nextSender;
};

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
    pushToast("操作失败：连接已断开。", "error", 3000);
  }
  return sent;
};

export const sendConversationDeleteCommand = async (
  command: Extract<ClientCommand, { type: "conversation.delete" }>,
): Promise<boolean> => {
  const resultPromise = sendClientCommandAwaitResult(
    command,
    "conversation.delete",
    // Isolated-worktree deletion may include a recoverable snapshot and
    // several bounded git operations.  Give that authoritative backend fence
    // the long-operation budget instead of reporting a false failure at the
    // generic 60-second command timeout.
    { silent: true, timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
  );
  if (isDesktop()) {
    void cleanupDesktopConversationResources(command.conversation_id);
  }
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

const cleanupDesktopConversationResources = async (conversationId: string): Promise<void> => {
  const operations: Array<[string, () => Promise<unknown>]> = [
    ["终端", () => Promise.resolve(ptyKillConversation(conversationId))],
  ];
  if (typeof embeddedBrowserCloseConversation === "function") {
    operations.push(["内嵌浏览器", () => Promise.resolve(embeddedBrowserCloseConversation(conversationId))]);
  }
  await Promise.all(operations.map(async ([label, operation]) => {
    try {
      await withLocalCleanupDeadline(operation(), label);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      pushToast(`${label}清理未完成，但会话删除已继续：${detail}`, "warning", 5000);
    }
  }));
};

const withLocalCleanupDeadline = <T>(promise: Promise<T>, label: string): Promise<T> => (
  new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`${label}清理超过 ${LOCAL_CONVERSATION_CLEANUP_TIMEOUT_MS / 1000} 秒`));
    }, LOCAL_CONVERSATION_CLEANUP_TIMEOUT_MS);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  })
);

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
    reject(new Error("连接已断开"));
  });
};

/**
 * Submit a blocking-prompt response using the acknowledgement semantics owned
 * by its wire protocol. Legacy approval/answer commands return a
 * `command.result`; low-level control responses deliberately do not, because
 * the originating request (or its terminal cancellation event) is the
 * observable state transition.
 */
export const sendPromptResponseCommand = async (
  command: ClientCommand,
): Promise<CommandResultEvent | null> => {
  if (command.type === "control_response" || command.type === "control_cancel_request") {
    if (!sendClientCommand(command)) throw new Error("连接已断开");
    return null;
  }
  return sendClientCommandAwaitResult(command, command.type);
};

export const commandResultSucceeded = (event: CommandResultEvent): boolean => {
  const level = String(event.level || "").toLowerCase();
  return level !== "error" && level !== "failed";
};

export const resolveClientCommandResult = (event: CommandResultEvent): boolean => {
  const clientCommandId = typeof event.data?.client_command_id === "string"
    ? event.data.client_command_id
    : "";
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
