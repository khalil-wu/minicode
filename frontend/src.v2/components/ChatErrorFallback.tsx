/**
 * Error fallback component for ChatPane
 * Displays when the chat rendering encounters an unrecoverable error
 */
export const ChatErrorFallback = () => {
  const handleReload = () => {
    window.location.reload();
  };

  return (
    <div className="flex h-full w-full items-center justify-center bg-gray-950">
      <div className="max-w-md space-y-4 text-center">
        <TriangleAlert className="mx-auto h-12 w-12" style={{ color: "var(--state-warning)" }} strokeWidth={1.75} aria-hidden="true" />
        <h2 className="text-xl font-semibold text-gray-100">
          Chat Error
        </h2>
        <p className="text-sm text-gray-400">
          The chat interface encountered an error and couldn't recover.
          Try reloading the page.
        </p>
        <button
          onClick={handleReload}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
        >
          Reload Page
        </button>
      </div>
    </div>
  );
};
import { TriangleAlert } from "lucide-react";
