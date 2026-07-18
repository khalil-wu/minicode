/**
 * Error fallback component for Panels (Editor, Terminal, Diff, Browser, etc.)
 * Displays when a panel encounters an unrecoverable error
 */
export const PanelErrorFallback = ({ panelName }: { panelName?: string }) => {
  const handleReload = () => {
    window.location.reload();
  };

  return (
    <div className="flex h-full w-full items-center justify-center bg-gray-900">
      <div className="max-w-sm space-y-3 px-4 text-center">
        <TriangleAlert className="mx-auto h-10 w-10" style={{ color: "var(--state-warning)" }} strokeWidth={1.75} aria-hidden="true" />
        <h3 className="text-base font-semibold text-gray-100">
          {panelName ? `${panelName} Error` : "Panel Error"}
        </h3>
        <p className="text-xs text-gray-400">
          This panel encountered an error and couldn't recover.
        </p>
        <button
          onClick={handleReload}
          className="rounded-md bg-gray-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-600"
        >
          Reload Page
        </button>
      </div>
    </div>
  );
};
import { TriangleAlert } from "lucide-react";
