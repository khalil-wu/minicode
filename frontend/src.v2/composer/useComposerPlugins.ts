import { useEffect, useState } from "react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";

export interface ComposerPlugin {
  id: string;
  name: string;
  marketplace: string;
  displayName?: string;
  description?: string;
  shortDescription?: string;
  skill_count?: number;
  mcp_server_count?: number;
  enabled: boolean;
}

export function useComposerPlugins(open: boolean, workspaceRoot: string) {
  const [plugins, setPlugins] = useState<ComposerPlugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setPlugins([]);
    setLoading(true);
    setError("");
    void (async () => {
      try {
        const query = new URLSearchParams({ workspace_root: workspaceRoot });
        const response = await fetchWithTimeout(`${apiBase()}/api/plugins?${query}`, {
          headers: authHeaders(), signal: controller.signal,
        });
        if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), `HTTP ${response.status}`));
        const payload = await response.json() as { plugins: ComposerPlugin[] };
        if (!controller.signal.aborted) setPlugins(payload.plugins);
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [open, workspaceRoot, revision]);
  return { plugins, loading, error, refresh: () => setRevision(value => value + 1) };
}
