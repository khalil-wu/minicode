import type { ClientCommand } from "./events";

export type ApprovalAction = "approve" | "reject" | "partial";
export interface PromptOwner {
  conversationId?: string;
  turnId?: string;
  messageId?: string;
}

export interface ApprovalResponseOptions {
  decisions?: Record<string, "approved" | "rejected">;
  feedback?: string;
  plan?: string;
  commandPrompts?: Array<{ tool: "run_command"; prompt: string }>;
  owner?: PromptOwner;
}

const ownerFields = (owner?: PromptOwner) => ({
  ...(owner?.conversationId ? { conversation_id: owner.conversationId } : {}),
  ...(owner?.turnId ? { turn_id: owner.turnId } : {}),
  ...(owner?.messageId ? { message_id: owner.messageId } : {}),
});

export const buildApprovalResponseCommand = (
  requestId: string,
  action: ApprovalAction,
  options?: ApprovalResponseOptions,
): ClientCommand => {
  const trimmedFeedback = options?.feedback?.trim() || undefined;
  const plan = typeof options?.plan === "string" ? options.plan : undefined;
  const commandPrompts = options?.commandPrompts?.flatMap((item) => {
    const prompt = String(item?.prompt || "").trim();
    return item?.tool === "run_command" && prompt ? [{ tool: "run_command" as const, prompt }] : [];
  });
  const response: Record<string, unknown> = { action };
  if (options?.decisions) response.decisions = options.decisions;
  if (trimmedFeedback) response.feedback = trimmedFeedback;
  if (plan !== undefined) response.plan = plan;
  if (commandPrompts?.length) response.command_prompts = commandPrompts;
  return {
    type: "control_response",
    request_id: requestId,
    ...ownerFields(options?.owner),
    response: {
      subtype: "success",
      response,
    },
  };
};

export const buildAskUserResponseCommand = (
  requestId: string,
  answer: string,
  owner?: PromptOwner,
): ClientCommand => {
  return {
    type: "control_response",
    request_id: requestId,
    ...ownerFields(owner),
    response: {
      subtype: "success",
      response: { answer },
    },
  };
};
