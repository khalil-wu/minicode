import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";

export const handleControlEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "approval_request": {
      const hasDiff = e.diff != null;
      if (hasDiff) {
        const diffData = e.diff as { files?: { path?: string; patch?: string | null; additions?: number; deletions?: number; is_large?: boolean; is_truncated?: boolean }[] } | string;
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
          s.updateToolCall(e.tool_call_id, { diff: { plus, minus, patch } });
        }
        s.setDiffReviewState({
          requestId: e.tool_call_id,
          toolName: e.tool_name,
          diff: patch || (typeof e.diff === "string" ? e.diff : JSON.stringify(e.diff, null, 2)),
          files,
          selectedPath: files[0]?.path,
          status: "pending",
          fileDecisions: {},
          lineComments: [],
        });
        s.addPanel({ id: "approval-diff", kind: "diff", label: "Diff Review" });
        s.setDiffReview({
          requestId: e.tool_call_id,
          diff: typeof e.diff === "string" ? e.diff : JSON.stringify(e.diff, null, 2),
        });
      } else {
        s.setApproval({
          requestId: e.tool_call_id,
          toolName: e.tool_name,
          args: e.args ?? {},
        });
      }
      return true;
    }
    case "approval.file_diff": {
      const ev = e as unknown as {
        tool_call_id?: string;
        path?: string;
        patch?: string;
        is_large?: boolean;
        is_truncated?: boolean;
      };
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
        useAppStore.setState((state) => ({
          pendingDiffReview: requestIds.includes(state.pendingDiffReview?.requestId ?? "")
            ? null
            : state.pendingDiffReview,
          diffReview: requestIds.includes(state.diffReview?.requestId ?? "")
            ? null
            : state.diffReview,
          pendingAskUser: requestIds.includes(state.pendingAskUser?.requestId ?? "")
            ? null
            : state.pendingAskUser,
        }));
      } else {
        useAppStore.setState({
          pendingApproval: null,
          approvalQueue: [],
          pendingDiffReview: null,
          diffReview: null,
          pendingAskUser: null,
        });
      }
      return true;
    }
    case "ask_user": {
      const ev = e as unknown as { tool_call_id?: string; request_id?: string; question?: string; options?: string[] };
      const requestId = ev.tool_call_id ?? ev.request_id ?? "";
      const question = ev.question ?? "The agent needs your input.";
      s.setAskUser({ requestId, question, options: ev.options });
      return true;
    }
    default:
      return false;
  }
};
