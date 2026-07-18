import { useEffect, useState, useCallback } from "react";
import { CheckCircle2, CircleAlert, Info, TriangleAlert, X } from "lucide-react";

export interface ToastItem {
  id: string;
  message: string;
  type: "info" | "success" | "warning" | "error";
  duration: number;
  createdAt: number;
}

let toastListeners: ((t: ToastItem) => void)[] = [];

export const pushToast = (message: string, type: ToastItem["type"] = "info", duration?: number) => {
  const text = message.trim();
  if (!text) return;
  const createdAt = Date.now();
  const item: ToastItem = {
    id: `t-${createdAt}-${Math.random().toString(36).slice(2, 5)}`,
    message: text,
    type,
    duration: duration ?? (type === "error" ? 6000 : type === "warning" ? 4500 : 2600),
    createdAt,
  };
  for (const fn of toastListeners) fn(item);
};

const TYPE_COLORS: Record<ToastItem["type"], string> = {
  info: "var(--state-info)",
  success: "var(--state-success)",
  warning: "var(--state-warning)",
  error: "var(--state-danger)",
};

const TYPE_ICONS: Record<ToastItem["type"], React.ReactNode> = {
  info: <Info size={15} />,
  success: <CheckCircle2 size={15} />,
  warning: <TriangleAlert size={15} />,
  error: <CircleAlert size={15} />,
};

export const ToastContainer = () => {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [exiting, setExiting] = useState<Set<string>>(new Set());

  useEffect(() => {
    const handler = (t: ToastItem) => setToasts((prev) => {
      const deduped = prev.filter((item) => item.message !== t.message || item.type !== t.type);
      return [...deduped.slice(-2), t];
    });
    toastListeners.push(handler);
    return () => { toastListeners = toastListeners.filter((h) => h !== handler); };
  }, []);

  const dismiss = useCallback((id: string) => {
    setExiting((prev) => new Set(prev).add(id));
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
      setExiting((prev) => { const next = new Set(prev); next.delete(id); return next; });
    }, 160);
  }, []);

  useEffect(() => {
    const timers: number[] = [];
    for (const t of toasts) {
      if (!exiting.has(t.id) && t.duration) {
        const remaining = Math.max(0, t.duration - (Date.now() - t.createdAt));
        timers.push(window.setTimeout(() => dismiss(t.id), remaining));
      }
    }
    return () => timers.forEach(clearTimeout);
  }, [toasts, exiting, dismiss]);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container" aria-live="polite" aria-relevant="additions removals">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast-card ${exiting.has(t.id) ? "toast-exit" : "toast-enter"}`}
          data-type={t.type}
          role={t.type === "error" || t.type === "warning" ? "alert" : "status"}
          title={t.message}
          style={{ "--toast-tone": TYPE_COLORS[t.type] } as React.CSSProperties}
        >
          <span className="toast-icon" aria-hidden="true">
            {TYPE_ICONS[t.type]}
          </span>
          <span className="toast-message">{t.message}</span>
          {(t.type === "error" || t.type === "warning" || t.duration === 0) && (
            <button
              type="button"
              aria-label="Dismiss notification"
              onClick={() => dismiss(t.id)}
              className="toast-dismiss"
            >
              <X size={14} />
            </button>
          )}
        </div>
      ))}
    </div>
  );
};
