import { useEffect, useRef, useState } from "react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { subscribeExtensionSettingsChanges } from "../lib/extension-settings-events";

export interface ComposerPlugin {
  id: string;
  name: string;
  marketplace: string;
  displayName?: string;
  description?: string;
  shortDescription?: string;
  path: string;
  iconUrl?: string;
  iconVariant?: "composer" | "logo" | "logo-dark";
  iconVariants?: ("composer" | "logo" | "logo-dark")[];
  skill_count?: number;
  mcp_server_count?: number;
  enabled: boolean;
}

export function useComposerPlugins(open: boolean, workspaceRoot: string) {
  const [plugins, setPlugins] = useState<ComposerPlugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const request = useRef<AbortController>();
  useEffect(() => {
    if (!open) return;
    return subscribeExtensionSettingsChanges("plugins", request, () => {
      request.current?.abort();
      setRevision(value => value + 1);
    });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    request.current = controller;
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
        const payload = await response.json() as { plugins: ComposerPlugin[]; plugins_enabled?: boolean };
        if (!controller.signal.aborted) setPlugins(payload.plugins.map(plugin => ({ ...plugin, enabled: plugin.enabled && payload.plugins_enabled !== false })));
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
