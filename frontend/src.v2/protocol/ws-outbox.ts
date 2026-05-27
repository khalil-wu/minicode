import type { ClientCommand } from "./events";
import { pushToast } from "../overlays/ToastContainer";

type Sender = (command: ClientCommand) => boolean;

let sender: Sender | null = null;

export const registerWebSocketSender = (nextSender: Sender | null) => {
  sender = nextSender;
};

export const sendClientCommand = (command: ClientCommand): boolean => {
  if (!sender) {
    pushToast("Operation failed: connection is offline.", "error", 3000);
    return false;
  }
  const sent = sender(command);
  if (!sent) {
    pushToast("Operation failed: connection is offline.", "error", 3000);
  }
  return sent;
};
