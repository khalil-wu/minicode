import { useEffect, useState, useCallback } from "react";
import { CheckCircle, XCircle, Info, AlertTriangle } from "lucide-react";
import { useAppStore } from "../stores";

export interface ToastItem {
  id: string;
  message: string;
  type: "info" | "success" | "warning" | "error";
  duration?: number;
}

let toastListeners: ((t: ToastItem) => void)[] = [];

export const pushToast = (message: string, type: ToastItem["type"] = "info", duration = 4000) => {
  const item: ToastItem = { id: `t-${Date.now()}-${Math.random().toString(36).slice(2, 5)}`, message, type, duration };
  for (const fn of toastListeners) fn(item);
};

const TYPE_COLORS: Record<ToastItem["type"], string> = {
  info: "var(--state-info)",
  success: "var(--state-success)",
  warning: "var(--state-warning)",
  error: "var(--state-danger)",
};

const TYPE_ICONS: Record<ToastItem["type"], React.ReactNode> = {
  success: <CheckCircle size={16} />,
  error: <XCircle size={16} />,
  info: <Info size={16} />,
  warning: <AlertTriangle size={16} />,
};

export const ToastContainer = () => {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [exiting, setExiting] = useState<Set<string>>(new Set());

  useEffect(() => {
    const handler = (t: ToastItem) => setToasts((prev) => [...prev.slice(-4), t]);
    toastListeners.push(handler);
    return () => { toastListeners = toastListeners.filter((h) => h !== handler); };
  }, []);

  const dismiss = useCallback((id: string) => {
    setExiting((prev) => new Set(prev).add(id));
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
      setExiting((prev) => { const next = new Set(prev); next.delete(id); return next; });
    }, 200);
  }, []);

  useEffect(() => {
    const timers: number[] = [];
    for (const t of toasts) {
      if (!exiting.has(t.id) && t.duration) {
        timers.push(window.setTimeout(() => dismiss(t.id), t.duration));
      }
    }
    return () => timers.forEach(clearTimeout);
  }, [toasts, exiting, dismiss]);

  if (toasts.length === 0) return null;

  return (
    <div
      className="toast-container"
      style={{
        position: "fixed",
        top: 56,
        right: 16,
        zIndex: "var(--z-toast)",  // 🆕 使用统一的 z-index 变量
        display: "flex",
        flexDirection: "column",
        gap: 8,
        maxWidth: 360,
        pointerEvents: "none",  // 🆕 允许点击穿透到背景
      }}
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          className={exiting.has(t.id) ? "toast-exit" : "toast-enter"}
          onClick={() => dismiss(t.id)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            background: "var(--surface-raised)",
            border: "1px solid var(--border-subtle)",
            borderLeft: `3px solid ${TYPE_COLORS[t.type]}`,
            borderRadius: "var(--radius-sm, 6px)",
            boxShadow: "var(--shadow-md)",
            padding: "10px 14px",
            fontSize: "var(--text-sm)",
            color: "var(--text-primary)",
            cursor: "pointer",
            lineHeight: 1.4,
            pointerEvents: "auto",  // 🆕 但 toast 本身可点击
          }}
        >
          <span style={{ color: TYPE_COLORS[t.type], flexShrink: 0, display: "flex" }}>
            {TYPE_ICONS[t.type]}
          </span>
          <span style={{ flex: 1 }}>{t.message}</span>
        </div>
      ))}
    </div>
  );
};
