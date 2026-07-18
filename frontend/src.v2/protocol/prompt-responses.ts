import type { ClientCommand } from "./events";

export type PromptProtocol = "legacy" | "control";
export type ApprovalAction = "approve" | "reject" | "partial";

export const buildApprovalResponseCommand = (
  requestId: string,
  action: ApprovalAction,
  protocol?: PromptProtocol,
  decisions?: Record<string, "approved" | "rejected">,
  feedback?: string,
): ClientCommand => {
  const trimmedFeedback = feedback?.trim() || undefined;
  if (protocol === "control") {
    const response: Record<string, unknown> = { action };
    if (decisions) response.decisions = decisions;
    if (trimmedFeedback) response.feedback = trimmedFeedback;
    return {
      type: "control_response",
      request_id: requestId,
      response: {
        subtype: "success",
        response,
      },
    };
  }
  return {
    type: "approval",
    tool_call_id: requestId,
    action,
    ...(decisions ? { decisions } : {}),
    ...(trimmedFeedback ? { feedback: trimmedFeedback } : {}),
  };
};

export const buildAskUserResponseCommand = (
  requestId: string,
  answer: string,
  protocol?: PromptProtocol,
): ClientCommand => {
  if (protocol === "control") {
    return {
      type: "control_response",
      request_id: requestId,
      response: {
        subtype: "success",
        response: { answer },
      },
    };
  }
  return {
    type: "answer",
    tool_call_id: requestId,
    answer,
  };
};
