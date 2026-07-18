/**
 * Error fallback component for Composer
 * Displays when the composer encounters an unrecoverable error
 */
export const ComposerErrorFallback = () => {
  const handleReload = () => {
    window.location.reload();
  };

  return (
    <div className="flex h-full w-full items-center justify-center border-t border-gray-800 bg-gray-900">
      <div className="max-w-md space-y-3 px-4 text-center">
        <TriangleAlert className="mx-auto h-10 w-10" style={{ color: "var(--state-warning)" }} strokeWidth={1.75} aria-hidden="true" />
        <h3 className="text-base font-semibold text-gray-100">
          Composer Error
        </h3>
        <p className="text-xs text-gray-400">
          The message input encountered an error. Try reloading the page.
        </p>
        <button
          onClick={handleReload}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
        >
          Reload Page
        </button>
      </div>
    </div>
  );
};
import { TriangleAlert } from "lucide-react";
