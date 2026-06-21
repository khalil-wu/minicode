export const NEW_TERMINAL_SESSION_EVENT = "minicode:new-terminal";

let pendingNewTerminalSession = false;

export const requestNewTerminalSession = () => {
  pendingNewTerminalSession = true;
  window.dispatchEvent(new CustomEvent(NEW_TERMINAL_SESSION_EVENT));
};

export const consumeNewTerminalSessionRequest = (): boolean => {
  if (!pendingNewTerminalSession) return false;
  pendingNewTerminalSession = false;
  return true;
};

export const hasPendingNewTerminalSessionRequest = (): boolean => pendingNewTerminalSession;
