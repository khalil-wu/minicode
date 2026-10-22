import { useEffect, useMemo, useRef, useState } from "react";
import { getWebSocket } from "./useWebSocket";
import { useAppStore } from "../stores";
import { readComputerUseSettings, removeComputerUseAppApproval, type ComputerUseApprovedApp,
  type ComputerUseSettingsOwner, type ComputerUseSettingsSnapshot } from "../protocol/computer-use-settings";

type Scope = { owner: ComputerUseSettingsOwner; lifetime: AbortController; request: number; busy: boolean; data: ComputerUseSettingsSnapshot | null };
type State = { scope: Scope | null; data: ComputerUseSettingsSnapshot | null; loading: boolean; error: string; removing: string | null };

export function useComputerUseSettings(active: boolean) {
  const connected = useAppStore(state => state.isConnected);
  const conversationId = useAppStore(state => state.conversationId);
  const workingDirectory = useAppStore(state => state.workingDirectory);
  const registryVersion = useAppStore(state => state.runtimeCapabilities?.mcp_registry_version);
  const sessionId = connected ? getWebSocket()?.sessionId : undefined;
  const owner = useMemo<ComputerUseSettingsOwner | null>(() => active && sessionId ? {
    session_id: sessionId, conversation_id: conversationId, workspace_root: workingDirectory || null,
  } : null, [active, sessionId, conversationId, workingDirectory, registryVersion]);
  const current = useRef<Scope | null>(null);
  const [state, setState] = useState<State>({ scope: null, data: null, loading: false, error: "", removing: null });
  const view: State = state.scope?.owner === owner && state.scope === current.current ? state
    : { scope: null, data: null, loading: owner !== null, error: "", removing: null };

  useEffect(() => {
    if (!owner) { current.current = null; setState({ scope: null, data: null, loading: false, error: "", removing: null }); return; }
    const scope: Scope = { owner, lifetime: new AbortController(), request: 0, busy: false, data: null };
    current.current = scope;
    const read = () => {
      if (scope.busy) return;
      const request = ++scope.request;
      setState({ scope, data: scope.data, loading: true, error: "", removing: null });
      void readComputerUseSettings(scope.owner, scope.lifetime.signal).then(data => {
        if (current.current !== scope || scope.lifetime.signal.aborted || request !== scope.request) return;
        scope.data = data;
        setState({ scope, data, loading: false, error: "", removing: null });
      }, error => {
        if (current.current !== scope || scope.lifetime.signal.aborted || request !== scope.request) return;
        setState({ scope, data: scope.data, loading: false, error: error instanceof Error ? error.message : String(error), removing: null });
      });
    };
    read();
    window.addEventListener("focus", read);
    return () => { scope.lifetime.abort(); if (current.current === scope) current.current = null; window.removeEventListener("focus", read); };
  }, [owner]);

  const remove = async (app: ComputerUseApprovedApp): Promise<boolean> => {
    const scope = view.scope;
    const snapshot = view.data!;
    if (!scope || current.current !== scope || scope.lifetime.signal.aborted
      || scope.data?.provider_id !== snapshot.provider_id || scope.data.selection_id !== snapshot.selection_id) return false;
    scope.busy = true;
    ++scope.request;
    setState({ scope, data: scope.data, loading: false, error: "", removing: app.app_id });
    try {
      const data = await removeComputerUseAppApproval(scope.owner, snapshot, app, scope.lifetime.signal);
      if (current.current !== scope || scope.lifetime.signal.aborted) return false;
      scope.data = data;
      setState({ scope, data, loading: false, error: "", removing: null });
      return true;
    } catch (error) {
      if (current.current !== scope || scope.lifetime.signal.aborted) return false;
      setState({ scope, data: scope.data, loading: false, error: error instanceof Error ? error.message : String(error), removing: null });
      return false;
    } finally { scope.busy = false; }
  };
  return { ...view, remove };
}
