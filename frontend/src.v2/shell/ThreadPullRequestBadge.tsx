import { useEffect, useRef, useState } from "react";
import { GitMerge, GitPullRequest, GitPullRequestClosed, GitPullRequestDraft } from "../lib/icons";
import { workspacePathsEqual } from "../lib/workspace-path";
import { openExternal } from "../desktop/runtime";
import { Tip } from "../components/Tooltip";
import { useI18n } from "../i18n";
import { getWebSocket } from "../hooks/useWebSocket";
import { threadMetadataRequest, type ThreadResourceReference } from "../protocol/thread-metadata";
import type { ConversationMeta } from "../stores/types";
import { useAppStore } from "../stores";
import { fireConfetti } from "../desktop/confetti";
import { pushToast } from "../overlays/ToastContainer";

export type LinkedPullRequest = {
  number: number; title: string; state: "OPEN" | "MERGED" | "CLOSED"; isDraft: boolean;
  url: string; headRefName: string; statusCheckRollup: Record<string, unknown>[] | null;
};
type PullRequestProjection = { referenceId: string; pr: LinkedPullRequest | null; error?: string };

/** One producer per visible conversation, shared by its project/recent rows. */
export function useThreadPullRequestStatuses(conversations: ConversationMeta[], connected: boolean) {
  const [statuses, setStatuses] = useState<Record<string, PullRequestProjection>>({});
  const previousPullRequests = useRef(new Map<string, LinkedPullRequest["state"]>());
  const sessionId = connected ? getWebSocket()?.sessionId : undefined;
  const owners = [...new Map(conversations.map(conversation => [conversation.id, conversation])).values()].flatMap(conversation => {
    const references = conversation.resourceReferences?.filter(item => item.attachment_type === "pull_request")
      .sort((left, right) => right.created_at - left.created_at || right.id.localeCompare(left.id)) ?? [];
    return references.length ? [{ id: conversation.id, branch: conversation.gitBranch,
      directory: conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot, references }] : [];
  });
  const signature = JSON.stringify(owners);
  useEffect(() => {
    if (!sessionId) return;
    let request: AbortController | null = null;
    setStatuses(previous => Object.fromEntries(Object.entries(previous).filter(([id, result]) =>
      owners.some(owner => owner.id === id && owner.references.some(reference => reference.id === result.referenceId)))));
    const refresh = () => {
      request?.abort();
      const controller = new AbortController();
      request = controller;
      for (const owner of owners) void (async () => {
        const received: Array<{ reference: ThreadResourceReference; pr: LinkedPullRequest }> = [];
        let firstError: string | undefined;
        const preferred = (result: typeof received[number]) => !!owner.branch && result.pr.headRefName === owner.branch
          && !!result.reference.payload && typeof result.reference.payload === "object"
          && workspacePathsEqual((result.reference.payload as Record<string, unknown>).root, owner.directory);
        for (const reference of owner.references) {
          try {
            const result = await threadMetadataRequest<{ reference_id: string; conversation_id: string; pr: LinkedPullRequest }>(
              `/${encodeURIComponent(owner.id)}/resources/${encodeURIComponent(reference.id)}/pull-request`,
              { signal: controller.signal }, sessionId);
            if (controller.signal.aborted) return;
            const identity = `${owner.id}:${reference.id}:${result.pr.url}`;
            const prior = previousPullRequests.current.get(identity);
            previousPullRequests.current.set(identity, result.pr.state);
            if (!controller.signal.aborted && prior === "OPEN" && result.pr.state === "MERGED"
              && useAppStore.getState().conversationId === owner.id) {
              void fireConfetti(useAppStore.getState().reducedMotion).catch(error => pushToast(String(error), "error"));
            }
            received.push({ reference, pr: result.pr });
            const current = received[received.length - 1];
            if (current.pr.state === "OPEN" && !current.pr.isDraft && (!owner.branch || !owner.directory || preferred(current))) break;
          } catch (error) {
            if (controller.signal.aborted) return;
            firstError ??= error instanceof Error ? error.message : String(error);
          }
        }
        if (controller.signal.aborted) return;
        const selected = received.find(result => preferred(result) && result.pr.state === "OPEN" && !result.pr.isDraft)
          ?? received.find(result => result.pr.state === "OPEN" && !result.pr.isDraft)
          ?? received.find(result => preferred(result) && result.pr.state === "OPEN")
          ?? received.find(result => result.pr.state === "OPEN") ?? received[0];
        setStatuses(previous => ({ ...previous, [owner.id]: selected
          ? { referenceId: selected.reference.id, pr: selected.pr }
          : { referenceId: owner.references[0].id, pr: null, error: firstError } }));
      })();
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => { request?.abort(); window.removeEventListener("focus", refresh); };
  }, [sessionId, signature]);
  return statuses;
}

export function ThreadPullRequestBadge({ pr }: { pr: LinkedPullRequest }) {
  const { t } = useI18n();
  const Icon = pr.state === "MERGED" ? GitMerge : pr.state === "CLOSED" ? GitPullRequestClosed
    : pr.isDraft ? GitPullRequestDraft : GitPullRequest;
  const label = pr.state === "MERGED" ? t("已合并 PR") : pr.state === "CLOSED" ? t("已关闭 PR")
    : pr.isDraft ? t("草稿 PR") : t("开放的 PR");
  return <Tip content={<span>{pr.title || label}</span>}>
    <button type="button" className="btn-ghost mc-thread-pr-badge" aria-label={`${label}: ${pr.title}`}
      style={{ display: "inline-flex", padding: 2, border: 0, color: pr.state === "MERGED" ? "var(--state-info)"
        : pr.state === "CLOSED" ? "var(--text-muted)" : pr.isDraft ? "var(--text-muted)" : "var(--state-success)" }}
      onClick={event => { event.stopPropagation(); void openExternal(pr.url); }}><Icon size={14} aria-hidden="true" /></button>
  </Tip>;
}
