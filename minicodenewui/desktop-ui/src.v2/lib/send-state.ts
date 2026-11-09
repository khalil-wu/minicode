export type SendButtonState = "idle" | "sending" | "queue" | "offline-queue" | "stop" | "disabled";

export interface SendStateInputs {
  hasContent: boolean;
  isStreaming: boolean;
  isConnected: boolean;
  hasModel?: boolean;
}

export const deriveSendState = (i: SendStateInputs): SendButtonState => {
  if (!i.isConnected) return i.hasContent && i.hasModel !== false ? "offline-queue" : "disabled";
  if (i.isStreaming) return i.hasContent ? "queue" : "stop";
  if (i.hasModel === false) return "disabled";
  if (i.hasContent) return "idle";
  return "disabled";
};
