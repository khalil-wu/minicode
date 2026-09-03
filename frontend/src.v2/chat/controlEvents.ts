import { useAppStore } from "../stores";
import type {
  ApprovalFileDiffEvent,
  ControlRequestEvent,
  ServerEvent,
} from "../protocol/events";
import type { DiffReviewState, PendingAskUserOption, PendingDiffReview } from "../stores/types";
import { diffFilePathsEqual } from "./diffReviewState";

const eventConversationId = (e: ServerEvent): string | undefined => {
  const conversationId = (e as unknown as { conversation_id?: unknown }).conversation_id;
  if (typeof conversationId === "string" && conversationId.trim()) return conversationId.trim();
  return undefined;
};

type ApprovalDiffData =
  | { files?: { path?: string; patch?: string | null; additions?: number; deletions?: number; is_large?: boolean; is_truncated?: boolean }[] }
  | string;

const choiceText = (value: unknown): string => {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "";
};

const extractChoiceOptions = (value: unknown): PendingAskUserOption[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const seen = new Set<string>();
  const options = value
    .map((item) => {
      if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") {
        const text = choiceText(item);
        return text ? { label: text, value: text } : null;
      }
      if (!item || typeof item !== "object") return null;
      const choice = item as {
        label?: unknown;
        title?: unknown;
        name?: unknown;
        value?: unknown;
        id?: unknown;
        description?: unknown;
        detail?: unknown;
        help?: unknown;
      };
      const label = [choice.label, choice.title, choice.name, choice.value, choice.id]
        .map(choiceText)
        .find(Boolean) ?? "";
      const optionValue = [choice.value, choice.id, choice.label, choice.title, choice.name]
        .map(choiceText)
        .find(Boolean) ?? "";
      if (!label || !optionValue) return null;
      const description = [choice.description, choice.detail, choice.help]
        .map(choiceText)
        .find((candidate) => Boolean(candidate) && candidate !== label);
      return {
        label,
        value: optionValue,
        ...(description ? { description } : {}),
      };
    })
    .filter((option): option is PendingAskUserOption => {
      if (!option) return false;
      const identity = `${option.label}\u0000${option.value}`;
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    });
  return options.length > 0 ? options : undefined;
};

const updateReviewFile = (
  review: DiffReviewState | undefined,
  event: ApprovalFileDiffEvent,
  workspaceRoot: unknown = "",
): DiffReviewState | undefined => {
  if (!review || review.requestId !== event.tool_call_id || !event.path || !event.patch) return review;
  const eventOwner = event as ApprovalFileDiffEvent & { conversation_id?: string; turn_id?: string };
  if (review.conversationId && eventOwner.conversation_id !== review.conversationId) return review;
  if (review.turnId && eventOwner.turn_id !== review.turnId) return review;
  const filePatch = {
    patch: event.patch,
    isLarge: event.is_large,
    isTruncated: event.is_truncated,
  };
  const hasFile = review.files.some((file) => diffFilePathsEqual(file.path, event.path, workspaceRoot));
  const files = hasFile
    ? review.files.map((file) => diffFilePathsEqual(file.path, event.path, workspaceRoot) ? { ...file, ...filePatch } : file)
    : [...review.files, { path: event.path, ...filePatch }];
  return {
    ...review,
    files,
    diff: diffFilePathsEqual(review.selectedPath, event.path, workspaceRoot) ? event.patch : review.diff,
  };
};

const updatePendingReviewFile = (
  pending: PendingDiffReview | null,
  event: ApprovalFileDiffEvent,
  workspaceRoot: unknown = "",
): PendingDiffReview | null => {
  if (!pending || pending.requestId !== event.tool_call_id) return pending;
  const reviewState = updateReviewFile(pending.reviewState, event, workspaceRoot);
  const reviewUpdated = Boolean(reviewState && reviewState !== pending.reviewState);
  return {
    ...pending,
    ...(reviewUpdated ? { reviewState } : {}),
    ...(reviewUpdated && diffFilePathsEqual(reviewState?.selectedPath, event.path, workspaceRoot) ? { diff: event.patch } : {}),
  };
};

const applyApprovalRequest = (
  request: {
    requestId: string;
    conversationId?: string;
    turnId?: string;
    messageId?: string;
    toolName: string;
    args: Record<string, unknown>;
    sourceAgent?: string;
    sourceThread?: string;
    sourceTool?: string;
    diff?: unknown;
    expiresAt?: number;
  },
) => {
  const s = useAppStore.getState();
  const hasDiff = request.diff != null;
  if (!hasDiff) {
    s.setApproval({
      requestId: request.requestId,
      conversationId: request.conversationId,
      turnId: request.turnId,
      messageId: request.messageId,
      toolName: request.toolName,
      args: request.args,
      sourceAgent: request.sourceAgent,
      sourceThread: request.sourceThread,
      sourceTool: request.sourceTool,
      expiresAt: request.expiresAt,
    });
    return;
  }

  const diffData = request.diff as ApprovalDiffData;
  let patch: string | undefined;
  let plus = 0;
  let minus = 0;
  let files: {
    path: string;
    patch?: string | null;
    additions?: number;
    deletions?: number;
    isLarge?: boolean;
    isTruncated?: boolean;
  }[] = [];
  if (typeof diffData === "object" && diffData.files) {
    patch = diffData.files.map((file) => file.patch ?? "").filter(Boolean).join("\n");
    for (const file of diffData.files) {
      plus += file.additions ?? 0;
      minus += file.deletions ?? 0;
    }
    files = diffData.files
      .filter((file) => typeof file.path === "string" && file.path.length > 0)
      .map((file) => ({
        path: file.path!,
        patch: file.patch,
        additions: file.additions,
        deletions: file.deletions,
        isLarge: file.is_large,
        isTruncated: file.is_truncated,
      }));
  } else if (typeof diffData === "string") {
    patch = diffData;
  }
  if (patch) {
    s.updateToolCall(
      request.requestId,
      { diff: { plus, minus, patch } },
      request.conversationId,
      undefined,
      request.messageId,
    );
  }
  const reviewState: DiffReviewState = {
    requestId: request.requestId,
    conversationId: request.conversationId,
    turnId: request.turnId,
    messageId: request.messageId,
    toolName: request.toolName,
    sourceAgent: request.sourceAgent,
    sourceThread: request.sourceThread,
    sourceTool: request.sourceTool,
    diff: patch || (typeof request.diff === "string" ? request.diff : JSON.stringify(request.diff, null, 2)),
    files,
    selectedPath: files[0]?.path,
    status: "pending",
    mode: "approval",
    fileDecisions: {},
    lineComments: [],
  };
  s.setDiffReview({
    requestId: request.requestId,
    conversationId: request.conversationId,
    turnId: request.turnId,
    messageId: request.messageId,
    sourceAgent: request.sourceAgent,
    sourceThread: request.sourceThread,
    sourceTool: request.sourceTool,
    expiresAt: request.expiresAt,
    diff: patch || (typeof request.diff === "string" ? request.diff : JSON.stringify(request.diff, null, 2)),
    filePath: files.length === 1 ? files[0]?.path : files.length > 1 ? `${files.length} files` : undefined,
    reviewState,
  });
};

export const handleControlEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "control_request": {
      const ev = e as ControlRequestEvent;
      const request = ev.request;
      const requestId = ev.request_id;
      const conversationId = eventConversationId(e);
      if (!conversationId) return true;
      if (request.subtype === "can_use_tool") {
        applyApprovalRequest({
          requestId,
          conversationId,
          turnId: ev.turn_id,
          messageId: ev.message_id,
          toolName: request.tool_name,
          args: request.input,
          sourceAgent: request.source_agent,
          sourceThread: request.source_thread,
          sourceTool: request.source_tool,
          expiresAt: ev.expires_at,
          diff: request.diff,
        });
        return true;
      }
      if (request.subtype === "elicitation") {
        s.setAskUser({
          requestId,
          conversationId,
          turnId: ev.turn_id,
          messageId: ev.message_id,
          prompt: request.prompt,
          question: request.question,
          inputSchema: request.schema,
          options: extractChoiceOptions(request.options ?? request.choices ?? request.allowed_values),
        });
        return true;
      }
      if (request.subtype === "provider_auth_prompt") {
        s.setAskUser({
          requestId,
          conversationId,
          turnId: ev.turn_id,
          messageId: ev.message_id,
          question: request.prompt,
          provider: request.provider,
          promptType: request.prompt_type,
          placeholder: request.placeholder,
          allowEmpty: request.allow_empty,
          allowCustom: request.allow_custom,
          secret: request.prompt_type === "secret",
          expiresAt: ev.expires_at,
          options: extractChoiceOptions(request.options),
        });
        return true;
      }
      return true;
    }
    case "approval.file_diff": {
      const ev = e as ApprovalFileDiffEvent;
      if (!eventConversationId(e)) return true;
      if (ev.path && ev.patch) {
        useAppStore.setState((state) => ({
          pendingDiffReview: updatePendingReviewFile(state.pendingDiffReview, ev, state.workingDirectory),
          diffReviewQueue: state.diffReviewQueue.map((item) =>
            updatePendingReviewFile(item, ev, state.workingDirectory) ?? item,
          ),
          diffReview: updateReviewFile(state.diffReview ?? undefined, ev, state.workingDirectory) ?? null,
        }));
      }
      return true;
    }
    case "approval.cancelled": {
      const ev = e as unknown as { conversation_id?: string; request_ids?: string[]; reason?: string };
      const conversationId = eventConversationId(e);
      // Cancellation is a conversation-owned terminal event.  An unowned
      // legacy payload must not clear every prompt in the renderer, because a
      // reconnect can deliver it after the user has switched conversations.
      if (!conversationId) return true;
      const requestIds = Array.isArray(ev.request_ids) ? ev.request_ids.filter(Boolean) : [];
      if (requestIds.length === 0) return true;
      const requested = new Set(requestIds);
      const owns = (prompt: { requestId?: string; conversationId?: string } | null | undefined) =>
        Boolean(prompt)
        && prompt?.conversationId === conversationId
        && requested.has(prompt.requestId ?? "");
      const state = useAppStore.getState();
      const approvalIds = [state.pendingApproval, ...state.approvalQueue]
        .filter(owns)
        .map((prompt) => prompt!.requestId);
      const diffIds = [state.pendingDiffReview, ...state.diffReviewQueue]
        .filter(owns)
        .map((prompt) => prompt!.requestId);
      const askIds = [state.pendingAskUser, ...state.askUserQueue]
        .filter(owns)
        .map((prompt) => prompt!.requestId);
      if (approvalIds.length > 0) s.clearApprovals(approvalIds);
      if (diffIds.length > 0) s.clearDiffReviews(diffIds);
      if (askIds.length > 0) s.clearAskUsers(askIds);
      useAppStore.setState((current) => ({
        diffReview: owns(current.diffReview) ? null : current.diffReview,
      }));
      return true;
    }
    default:
      return false;
  }
};
