import { useEffect, useState } from "react";
import { Radio } from "lucide-react";
import { useAppStore } from "../stores";
import { isProviderRequestProgress, providerProgressLabel } from "../lib/provider-progress";

/** Live request status stays outside the persisted conversation transcript. */
export function ProviderRequestStatus({ wide = false }: { wide?: boolean }) {
  const request = useAppStore((state) => {
    if (!state.isStreaming || !state.conversationId) return undefined;
    for (let index = state.agentProgress.length - 1; index >= 0; index -= 1) {
      const entry = state.agentProgress[index];
      if (entry.conversationId === state.conversationId && isProviderRequestProgress(entry)) {
        return entry.status === "running" ? entry : undefined;
      }
    }
    return undefined;
  });
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    if (!request) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [request?.id, request?.timestamp, Boolean(request)]);

  if (!request) return null;
  const seconds = Math.max(0, Math.floor((now - request.timestamp) / 1000));
  const elapsed = seconds >= 60
    ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
    : `${seconds}s`;
  const label = request.providerState === "responding"
    ? "Waiting for model"
    : providerProgressLabel(request) || request.message;

  return (
    <div
      className="provider-request-status"
      style={{ width: wide ? "var(--chat-wide-axis-width)" : "var(--chat-composer-axis-width)" }}
      data-provider-request-state={request.providerState}
      role="status"
      aria-live="polite"
      title="模型请求尚未结束，不代表工具仍在执行。若长时间没有输出，可点击右下角停止按钮取消后重试。"
    >
      <Radio size={14} strokeWidth={1.75} aria-hidden="true" />
      <span className="provider-request-status-label">{label}</span>
      <span className="provider-request-status-elapsed" aria-hidden="true">{elapsed}</span>
    </div>
  );
}
