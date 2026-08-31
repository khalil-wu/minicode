import type { ProgressContentBlock } from "../stores/types";

/** The provider retry fields describe one logical request ladder. */
export type ProviderProgressSnapshot = Pick<
  ProgressContentBlock,
  "id" | "status" | "retryAttempt" | "maxRetries" | "message" | "providerState"
>;

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
    && String(progress.id || "").startsWith("provider:")
    && (
      typeof progress.retryAttempt === "number"
      || typeof progress.maxRetries === "number"
      || Boolean(progress.providerState)
    )
  );
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
  if (!isProviderRetryProgress(progress)) return undefined;
  const providerState = progress?.providerState;
  const status = String(progress?.status || "").toLowerCase();
  const counter = providerRetryCounter(progress);
  if (providerState === "connecting") return "正在连接提供商";
  if (providerState === "reconnecting") {
    return counter ? `正在重新连接 ${counter}` : "正在重新连接";
  }
  if (providerState === "responding") return "模型正在响应";
  if (providerState === "failed") {
    return counter ? `连接失败（重试 ${counter} 后）` : "连接失败";
  }
  if (providerState === "interrupted") {
    return counter ? `连接中断（重试 ${counter}）` : "连接中断";
  }
  if (providerState === "completed") {
    return counter ? `提供商响应完成（重试 ${counter}）` : "提供商响应完成";
  }
  if (status === "running") {
    return counter ? `正在重新连接 ${counter}` : "正在连接提供商";
  }
  if (status === "failed") {
    return counter ? `连接失败（重试 ${counter} 后）` : "连接失败";
  }
  if (status === "partial" || status === "cancelled" || status === "interrupted") {
    return counter ? `连接中断（重试 ${counter}）` : "连接中断";
  }
  if (status === "completed" || status === "done" || status === "success") {
    return counter ? `提供商已连接（重试 ${counter}）` : "提供商已连接";
  }
  return counter ? `重新连接 ${counter}` : progress?.message || undefined;
}
