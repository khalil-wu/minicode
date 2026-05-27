export type SendButtonState = "idle" | "sending" | "stop" | "disabled";

export interface SendStateInputs {
  hasContent: boolean;
  isStreaming: boolean;
  isConnected: boolean;
}

export const deriveSendState = (i: SendStateInputs): SendButtonState => {
  if (!i.isConnected) return "disabled";
  if (i.isStreaming) return "stop";
  if (i.hasContent) return "idle";
  return "disabled";
};
