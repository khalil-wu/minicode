import type { ClientCommand } from "./events";
import { pushToast } from "../overlays/ToastContainer";

type Sender = (command: ClientCommand) => boolean;
type SendClientCommandOptions = { silent?: boolean };

let sender: Sender | null = null;

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
