export type SendButtonState = "idle" | "sending" | "stop" | "disabled";

export interface SendStateInputs {
  hasContent: boolean;
  isStreaming: boolean;
  isConnected: boolean;
  hasModel?: boolean;
}

export const deriveSendState = (i: SendStateInputs): SendButtonState => {
  if (!i.isConnected) return "disabled";
  if (i.isStreaming) return "stop";
  if (i.hasModel === false) return "disabled";
  if (i.hasContent) return "idle";
  return "disabled";
};
