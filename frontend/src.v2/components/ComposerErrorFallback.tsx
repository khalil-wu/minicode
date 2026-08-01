/**
 * Error fallback component for Composer
 * Displays when the composer encounters an unrecoverable error
 */
export const ComposerErrorFallback = () => {
  const handleReload = () => {
    window.location.reload();
  };

  return (
    <div className="flex h-full w-full items-center justify-center" style={{ borderTop: "1px solid var(--border-subtle)", background: "var(--surface-base)", color: "var(--text-primary)" }}>
      <div className="max-w-md space-y-3 px-4 text-center">
        <TriangleAlert className="mx-auto h-10 w-10" style={{ color: "var(--state-warning)" }} strokeWidth={1.75} aria-hidden="true" />
        <h3 className="text-base font-semibold">
          输入框加载失败
        </h3>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          消息输入框遇到错误，请重新加载页面。
        </p>
        <button
          onClick={handleReload}
          className="px-3 py-1.5 text-xs font-medium"
          style={{ border: 0, borderRadius: "var(--radius-md)", background: "var(--accent-primary)", color: "var(--text-on-accent)" }}
        >
          重新加载
        </button>
      </div>
    </div>
  );
};
import { TriangleAlert } from "lucide-react";
