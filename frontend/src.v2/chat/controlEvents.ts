import { useAppStore } from "../stores";
import type { ApprovalFileDiffEvent, ServerEvent } from "../protocol/events";
import type { DiffReviewState } from "../stores/types";

const eventConversationId = (e: ServerEvent): string | undefined => {
  const conversationId = (e as unknown as { conversation_id?: unknown }).conversation_id;
  return typeof conversationId === "string" && conversationId.trim() ? conversationId.trim() : undefined;
};

type ApprovalDiffData =
  | { files?: { path?: string; patch?: string | null; additions?: number; deletions?: number; is_large?: boolean; is_truncated?: boolean }[] }
  | string;

const extractChoiceOptions = (value: unknown): string[] | undefined => {
  if (!Array.isArray(value)) return undefined;
  const options = value
    .map((item) => {
      if (typeof item === "string") return item.trim();
      if (!item || typeof item !== "object") return "";
      const choice = item as { label?: unknown; title?: unknown; value?: unknown; id?: unknown };
      for (const candidate of [choice.label, choice.title, choice.value, choice.id]) {
        if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
      }
      return "";
    })
    .filter(Boolean);
  return options.length > 0 ? options : undefined;
};

const applyApprovalRequest = (
  request: {
    requestId: string;
    protocol?: "legacy" | "control";
    conversationId?: string;
    toolName: string;
    args: Record<string, unknown>;
    sourceAgent?: string;
    sourceThread?: string;
    sourceTool?: string;
    diff?: unknown;
  },
) => {
  const s = useAppStore.getState();
  const hasDiff = request.diff != null;
  if (!hasDiff) {
    s.setApproval({
      requestId: request.requestId,
      protocol: request.protocol,
      conversationId: request.conversationId,
      toolName: request.toolName,
      args: request.args,
      sourceAgent: request.sourceAgent,
      sourceThread: request.sourceThread,
      sourceTool: request.sourceTool,
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
    s.updateToolCall(request.requestId, { diff: { plus, minus, patch } });
  }
  const reviewState: DiffReviewState = {
    requestId: request.requestId,
    protocol: request.protocol,
    conversationId: request.conversationId,
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
    protocol: request.protocol,
    conversationId: request.conversationId,
    sourceAgent: request.sourceAgent,
    sourceThread: request.sourceThread,
    sourceTool: request.sourceTool,
    diff: patch || (typeof request.diff === "string" ? request.diff : JSON.stringify(request.diff, null, 2)),
    filePath: files.length === 1 ? files[0]?.path : files.length > 1 ? `${files.length} files` : undefined,
    reviewState,
  });
};

export const handleControlEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "control_request": {
      const ev = e as unknown as {
        request_id?: string;
        request?: {
          subtype?: string;
          tool_name?: string;
          input?: Record<string, unknown>;
          diff?: unknown;
          prompt?: string;
          question?: string;
          tool_use_id?: string;
          options?: unknown;
          choices?: unknown;
          allowed_values?: unknown;
          source_agent?: string;
          source_thread?: string;
          source_tool?: string;
        };
      };
      const request = ev.request ?? {};
      const requestId = ev.request_id || request.tool_use_id || "";
      const conversationId = eventConversationId(e);
      if (request.subtype === "can_use_tool") {
        applyApprovalRequest({
          requestId,
          protocol: "control",
          conversationId,
          toolName: request.tool_name || "tool",
          args: request.input ?? {},
          sourceAgent: request.source_agent,
          sourceThread: request.source_thread,
          sourceTool: request.source_tool,
          diff: request.diff,
        });
        return true;
      }
      if (request.subtype === "elicitation") {
        s.setAskUser({
          requestId,
          protocol: "control",
          conversationId,
          question: request.question || request.prompt || "The agent needs your input.",
          options: extractChoiceOptions(request.options ?? request.choices ?? request.allowed_values),
        });
        return true;
      }
      return true;
    }
    case "approval_request": {
      applyApprovalRequest({
        requestId: e.tool_call_id,
        conversationId: eventConversationId(e),
        toolName: e.tool_name,
        args: e.args ?? {},
        sourceAgent: (e as unknown as { source_agent?: string }).source_agent,
        sourceThread: (e as unknown as { source_thread?: string }).source_thread,
        sourceTool: (e as unknown as { source_tool?: string }).source_tool,
        diff: e.diff,
      });
      return true;
    }
    case "approval.file_diff": {
      const ev = e as ApprovalFileDiffEvent;
      if (ev.path && ev.patch) {
        s.updateDiffReviewFile(ev.path, {
          patch: ev.patch,
          isLarge: ev.is_large,
          isTruncated: ev.is_truncated,
        });
        const current = useAppStore.getState().diffReview;
        if (current && current.requestId === ev.tool_call_id && current.selectedPath === ev.path) {
          s.setDiffReviewState({ ...current, diff: ev.patch });
        }
      }
      return true;
    }
    case "approval.cancelled": {
      const ev = e as unknown as { request_ids?: string[]; reason?: string };
      const requestIds = Array.isArray(ev.request_ids) ? ev.request_ids.filter(Boolean) : [];
      if (requestIds.length > 0) {
        s.clearApprovals(requestIds);
        s.clearDiffReviews(requestIds);
        s.clearAskUsers(requestIds);
        useAppStore.setState((state) => ({
          diffReview: requestIds.includes(state.diffReview?.requestId ?? "")
            ? null
            : state.diffReview,
        }));
      } else {
        useAppStore.setState({
          pendingApproval: null,
          approvalQueue: [],
          pendingDiffReview: null,
          diffReviewQueue: [],
          diffReview: null,
          pendingAskUser: null,
          askUserQueue: [],
        });
      }
      return true;
    }
    case "ask_user": {
      const ev = e as unknown as { tool_call_id?: string; request_id?: string; question?: string; options?: string[] };
      const requestId = ev.tool_call_id ?? ev.request_id ?? "";
      const question = ev.question ?? "The agent needs your input.";
      s.setAskUser({ requestId, conversationId: eventConversationId(e), question, options: ev.options });
      return true;
    }
    default:
      return false;
  }
};
