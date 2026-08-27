import { useEffect, useRef, useState, useCallback } from "react";
import { CheckCircle2, CircleAlert, Info, TriangleAlert, X } from "lucide-react";

export interface ToastItem {
  id: string;
  message: string;
  type: "info" | "success" | "warning" | "error";
  duration: number;
  createdAt: number;
}

let toastListeners: ((t: ToastItem) => void)[] = [];
let toastDismissListeners: ((id: string) => void)[] = [];

export const pushToast = (
  message: string,
  type: ToastItem["type"] = "info",
  duration?: number,
): string | null => {
  const text = message.trim();
  if (!text) return null;
  const createdAt = Date.now();
  const item: ToastItem = {
    id: `t-${createdAt}-${Math.random().toString(36).slice(2, 5)}`,
    message: text,
    type,
    duration: duration ?? (type === "error" ? 6000 : type === "warning" ? 4500 : 2600),
    createdAt,
  };
  for (const fn of toastListeners) fn(item);
  return item.id;
};

export const dismissToast = (id: string | null | undefined): boolean => {
  const targetId = String(id || "").trim();
  if (!targetId) return false;
  for (const fn of toastDismissListeners) fn(targetId);
  return toastDismissListeners.length > 0;
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
  const [paused, setPaused] = useState<Set<string>>(new Set());
  const pausedAtRef = useRef(new Map<string, number>());

  const dismiss = useCallback((id: string) => {
    setExiting((prev) => new Set(prev).add(id));
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
      setExiting((prev) => { const next = new Set(prev); next.delete(id); return next; });
    }, 160);
  }, []);

  useEffect(() => {
    const addHandler = (t: ToastItem) => setToasts((prev) => {
      const deduped = prev.filter((item) => item.message !== t.message || item.type !== t.type);
      return [...deduped.slice(-2), t];
    });
    const dismissHandler = (id: string) => dismiss(id);
    toastListeners.push(addHandler);
    toastDismissListeners.push(dismissHandler);
    return () => {
      toastListeners = toastListeners.filter((handler) => handler !== addHandler);
      toastDismissListeners = toastDismissListeners.filter((handler) => handler !== dismissHandler);
    };
  }, [dismiss]);

  // Auto-dismiss timers restart from the remaining time whenever the paused
  // set changes — hovering a toast freezes its countdown instead of the old
  // "timer keeps running while you read" behavior.
  useEffect(() => {
    const timers: number[] = [];
    for (const t of toasts) {
      if (!exiting.has(t.id) && t.duration && !paused.has(t.id)) {
        const remaining = Math.max(0, t.duration - (Date.now() - t.createdAt));
        timers.push(window.setTimeout(() => dismiss(t.id), remaining));
      }
    }
    return () => timers.forEach(clearTimeout);
  }, [toasts, exiting, paused, dismiss]);

  const setToastPaused = useCallback((id: string, hovered: boolean) => {
    if (hovered) {
      pausedAtRef.current.set(id, Date.now());
      setPaused((prev) => new Set(prev).add(id));
    } else {
      const pausedAt = pausedAtRef.current.get(id);
      pausedAtRef.current.delete(id);
      setPaused((prev) => { const next = new Set(prev); next.delete(id); return next; });
      if (pausedAt != null) {
        // Push createdAt forward by the hovered span so the remaining
        // countdown resumes exactly where it froze.
        const elapsed = Date.now() - pausedAt;
        setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, createdAt: t.createdAt + elapsed } : t)));
      }
    }
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container" aria-live="polite" aria-relevant="additions removals">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`toast-card ${exiting.has(t.id) ? "toast-exit" : "toast-enter"}`}
          data-type={t.type}
          data-paused={paused.has(t.id) ? "true" : undefined}
          role={t.type === "error" || t.type === "warning" ? "alert" : "status"}
          style={{ "--toast-tone": TYPE_COLORS[t.type] } as React.CSSProperties}
          onMouseEnter={() => setToastPaused(t.id, true)}
          onMouseLeave={() => setToastPaused(t.id, false)}
        >
          <span className="toast-icon" aria-hidden="true">
            {TYPE_ICONS[t.type]}
          </span>
          <span className="toast-message">{t.message}</span>
          <button
            type="button"
            aria-label="关闭通知"
            onClick={() => dismiss(t.id)}
            className="toast-dismiss"
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  );
};
