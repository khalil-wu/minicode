/**
 * Error fallback component for Panels (Editor, Terminal, Diff, Browser, etc.)
 * Displays when a panel encounters an unrecoverable error
 */
export const PanelErrorFallback = ({ panelName }: { panelName?: string }) => {
  const handleReload = () => {
    window.location.reload();
  };

  return (
    <div className="flex h-full w-full items-center justify-center" style={{ background: "var(--surface-base)", color: "var(--text-primary)" }}>
      <div className="max-w-sm space-y-3 px-4 text-center">
        <TriangleAlert className="mx-auto h-10 w-10" style={{ color: "var(--state-warning)" }} strokeWidth={1.75} aria-hidden="true" />
        <h3 className="text-base font-semibold">
          {panelName ? `${panelName}面板加载失败` : "面板加载失败"}
        </h3>
        <p className="text-xs" style={{ color: "var(--text-muted)" }}>
          此面板遇到错误且无法恢复。
        </p>
        <button
          onClick={handleReload}
          className="px-3 py-1.5 text-xs font-medium"
          style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", background: "var(--surface-soft)", color: "var(--text-primary)" }}
        >
          重新加载
        </button>
      </div>
    </div>
  );
};
import { TriangleAlert } from "lucide-react";
