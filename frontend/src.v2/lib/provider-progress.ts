import type { ProgressContentBlock } from "../stores/types";

/** The provider retry fields describe one logical request ladder. */
export type ProviderProgressSnapshot = Pick<
  ProgressContentBlock,
  "id" | "status" | "retryAttempt" | "maxRetries" | "message" | "providerState"
> & Partial<Pick<ProgressContentBlock, "label" | "phase" | "errorMessage">>;

/** A stream failure notification is evidence, even when its request retries. */
const isProviderErrorNotice = (progress: ProviderProgressSnapshot | undefined): boolean =>
  progress?.phase === "recover"
  && Boolean(progress.errorMessage?.trim())
  && ["reconnecting", "failed"].includes(progress.providerState || "");

const PROVIDER_PROGRESS_STATUS_RANK: Record<string, number> = {
  "": 0,
  info: 0,
  running: 1,
  partial: 2,
  completed: 3,
  failed: 4,
};

const PROVIDER_PROGRESS_TERMINAL_STATUSES = new Set(["partial", "completed", "failed"]);

const PROVIDER_PROGRESS_STATE_RANK: Record<string, number> = {
  "": 0,
  connecting: 1,
  reconnecting: 2,
  responding: 3,
  completed: 4,
  failed: 4,
  interrupted: 4,
};

/** Reject a delayed provider frame that would move one retry row backwards. */
export function providerProgressLifecycleRegressed(
  previous: Pick<ProviderProgressSnapshot, "status" | "retryAttempt" | "providerState">,
  incoming: Pick<ProviderProgressSnapshot, "status" | "retryAttempt" | "providerState">,
): boolean {
  const previousStatus = PROVIDER_PROGRESS_STATUS_RANK[String(previous.status || "").toLowerCase()] ?? 0;
  const incomingStatus = PROVIDER_PROGRESS_STATUS_RANK[String(incoming.status || "").toLowerCase()] ?? 0;
  const previousAttempt = typeof previous.retryAttempt === "number" ? previous.retryAttempt : undefined;
  const incomingAttempt = typeof incoming.retryAttempt === "number" ? incoming.retryAttempt : undefined;
  const attemptRegressed = previousAttempt !== undefined
    && incomingAttempt !== undefined
    && incomingAttempt < previousAttempt;
  const stateRegressed = (
    (PROVIDER_PROGRESS_STATE_RANK[String(previous.providerState || "").toLowerCase()] ?? 0)
      > (PROVIDER_PROGRESS_STATE_RANK[String(incoming.providerState || "").toLowerCase()] ?? 0)
    && (
      previousAttempt === undefined
      || incomingAttempt === undefined
      || previousAttempt === incomingAttempt
    )
  );
  return previousStatus > incomingStatus
    || (
      PROVIDER_PROGRESS_TERMINAL_STATUSES.has(String(previous.status || "").toLowerCase())
      && !PROVIDER_PROGRESS_TERMINAL_STATUSES.has(String(incoming.status || "").toLowerCase())
    )
    || (previousStatus === incomingStatus && attemptRegressed)
    || stateRegressed;
}

export function isProviderRetryProgress(
  progress: ProviderProgressSnapshot | undefined,
): boolean {
  return Boolean(
    progress
    && (Boolean(progress.providerState)
      || (String(progress.id || "").startsWith("provider:") && (
      typeof progress.retryAttempt === "number"
      || typeof progress.maxRetries === "number"
    )))
  );
}

/**
 * Identify the provider-request retry ladder, as opposed to provider-owned
 * tool activity such as an MCP call or image-generation progress.  The latter
 * has a `provider:` id but does not carry retry/provider-state fields.
 */
export function isProviderRequestProgress(
  progress: ProviderProgressSnapshot | undefined,
): boolean {
  if (isProviderErrorNotice(progress)) return false;
  const id = String(progress?.id || "");
  return id.startsWith("provider-request:") || isProviderRetryProgress(progress)
    || (progress?.label === "provider" && ["model", "provider", "recover"].includes(progress.phase || ""));
}

/**
 * Provider request/retry lifecycle belongs to the transient status surface,
 * not the conversation transcript. Codex keeps the corresponding raw response
 * and retry notifications out of its user-facing item projection.
 *
 * Keep the semantic check independent of `visibility` so old transcripts that
 * persisted these rows as `timeline` are repaired by the current projection.
 */
export function isProviderCompletionProgress(
  progress: ProviderProgressSnapshot | undefined,
): boolean {
  const id = String(progress?.id || "");
  if (!isProviderRequestProgress(progress)) return false;
  const status = String(progress?.status || "").toLowerCase();
  const providerState = String(progress?.providerState || "").toLowerCase();
  return status === "completed"
    || status === "done"
    || status === "success"
    || providerState === "completed";
}

export function providerRetryCounter(
  progress: ProviderProgressSnapshot | undefined,
): string | undefined {
  if (!isProviderRetryProgress(progress)) return undefined;
  const attempt = progress?.retryAttempt;
  if (typeof attempt !== "number" || !Number.isFinite(attempt) || attempt <= 0) return undefined;
  const max = progress?.maxRetries;
  return `${attempt}/${typeof max === "number" && Number.isFinite(max) ? max : "?"}`;
}

/**
 * Return the one user-facing label for a provider request/retry row.
 *
 * Provider activity rows also use `provider:*` ids, but do not carry retry
 * fields. They deliberately return undefined so their normal tool labels stay
 * intact. The retry ladder is rendered from typed counters instead of parsing
 * provider prose, which keeps 1/N -> N/N monotonic across reconnects.
 */
export function providerProgressLabel(
  progress: ProviderProgressSnapshot | undefined,
): string | undefined {
  if (isProviderErrorNotice(progress)) return undefined;
  if (!isProviderRetryProgress(progress)) return undefined;
  const providerState = progress?.providerState;
  const status = String(progress?.status || "").toLowerCase();
  const counter = providerRetryCounter(progress);
  if (providerState === "connecting") return "Connecting";
  if (providerState === "reconnecting") {
    return counter ? `Reconnecting ${counter}` : "Reconnecting";
  }
  if (providerState === "responding") return "Waiting for model";
  if (providerState === "failed") {
    return counter ? `Connection failed after ${counter} retries` : "Connection failed";
  }
  if (providerState === "interrupted") {
    return counter ? `Connection interrupted (${counter})` : "Connection interrupted";
  }
  if (providerState === "completed") {
    return counter ? `Response completed (${counter})` : "Response completed";
  }
  if (status === "running") {
    return counter ? `Reconnecting ${counter}` : "Connecting";
  }
  if (status === "failed") {
    return counter ? `Connection failed after ${counter} retries` : "Connection failed";
  }
  if (status === "partial" || status === "cancelled" || status === "interrupted") {
    return counter ? `Connection interrupted (${counter})` : "Connection interrupted";
  }
  if (status === "completed" || status === "done" || status === "success") {
    return counter ? `Response completed (${counter})` : "Response completed";
  }
  return counter ? `Reconnecting ${counter}` : progress?.message || undefined;
}
