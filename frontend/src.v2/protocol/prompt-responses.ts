import type { ClientCommand } from "./events";
import type { AdditionalPermissionProfile } from "./common-types";

export type ApprovalAction = "approve" | "reject" | "partial";
export interface PromptOwner {
  conversationId?: string;
  turnId?: string;
  messageId?: string;
  taskId?: string;
}

export interface ApprovalResponseOptions {
  decisions?: Record<string, "approved" | "rejected">;
  feedback?: string;
  rememberForSession?: boolean;
  rememberForFuture?: boolean;
  execPolicyAmendment?: string[];
  requestDigest?: string;
  sourceCommandDecision?: "approved" | "abort";
  plan?: string;
  commandPrompts?: Array<{ tool: "run_command"; prompt: string }>;
  owner?: PromptOwner;
}

const ownerFields = (owner?: PromptOwner) => ({
  ...(owner?.conversationId ? { conversation_id: owner.conversationId } : {}),
  ...(owner?.turnId ? { turn_id: owner.turnId } : {}),
  ...(owner?.messageId ? { message_id: owner.messageId } : {}),
  ...(owner?.taskId ? { task_id: owner.taskId } : {}),
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
  if (action === "approve" && options?.rememberForSession) response.remember_for_session = true;
  if (action === "approve" && options?.rememberForFuture) response.remember_for_future = true;
  if (options?.requestDigest) response.request_digest = options.requestDigest;
  if (options?.sourceCommandDecision) response.decision = options.sourceCommandDecision;
  if (action === "approve" && options?.execPolicyAmendment) {
    response.decision = { approved_execpolicy_amendment: { proposed_execpolicy_amendment: [...options.execPolicyAmendment] } };
  }
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

export const buildPermissionsResponseCommand = (
  requestId: string,
  permissions: AdditionalPermissionProfile,
  scope: "turn" | "session",
  owner: PromptOwner,
): ClientCommand => ({
  type: "control_response",
  request_id: requestId,
  ...ownerFields(owner),
  response: { subtype: "success", response: { permissions, scope } },
});

export const buildElicitationResponseCommand = (
  requestId: string,
  action: "accept" | "decline" | "cancel",
  content: Record<string, unknown> | undefined,
  owner?: PromptOwner,
): ClientCommand => ({
  type: "control_response",
  request_id: requestId,
  ...ownerFields(owner),
  response: { subtype: "success", response: { action, ...(content ? { content } : {}) } },
});

export const buildRequestUserInputResponseCommand = (
  requestId: string,
  answers: Record<string, { answers: string[] }> | null,
  owner: PromptOwner,
): ClientCommand => ({
  type: "control_response",
  request_id: requestId,
  ...ownerFields(owner),
  response: { subtype: "success", response: answers === null ? { action: "cancel" } : { answers } },
});
