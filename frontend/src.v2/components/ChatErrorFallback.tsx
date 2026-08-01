/**
 * Error fallback component for ChatPane
 * Displays when the chat rendering encounters an unrecoverable error
 */
export const ChatErrorFallback = () => {
  const handleReload = () => {
    window.location.reload();
  };

  return (
    <div className="flex h-full w-full items-center justify-center" style={{ background: "var(--surface-base)", color: "var(--text-primary)" }}>
      <div className="max-w-md space-y-4 text-center">
        <TriangleAlert className="mx-auto h-12 w-12" style={{ color: "var(--state-warning)" }} strokeWidth={1.75} aria-hidden="true" />
        <h2 className="text-xl font-semibold">
          对话加载失败
        </h2>
        <p className="text-sm" style={{ color: "var(--text-muted)" }}>
          对话界面遇到错误且无法恢复，请重新加载页面。
        </p>
        <button
          onClick={handleReload}
          className="px-4 py-2 text-sm font-medium"
          style={{ border: 0, borderRadius: "var(--radius-md)", background: "var(--accent-primary)", color: "var(--text-on-accent)" }}
        >
          重新加载
        </button>
      </div>
    </div>
  );
};
import { TriangleAlert } from "lucide-react";
