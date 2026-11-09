import { useEffect, useMemo, useRef, useState } from "react";
import { getWebSocket } from "./useWebSocket";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useI18n } from "../i18n";
import { readComputerUseSettings, removeComputerUseAppApproval, updateComputerUsePlugin, type ComputerUseApprovedApp,
  type ComputerUsePluginAction, type ComputerUsePluginControl, type ComputerUseSettingsOwner,
  type ComputerUseSettingsSnapshot } from "../protocol/computer-use-settings";

type Scope = { owner: ComputerUseSettingsOwner; lifetime: AbortController; request: number; busy: boolean; data: ComputerUseSettingsSnapshot | null };
type State = { scope: Scope | null; data: ComputerUseSettingsSnapshot | null; loading: boolean; error: string;
  removing: string | null; pluginPending: string | null };
type Mutation = { kind: "remove"; app: ComputerUseApprovedApp }
  | { kind: "plugin"; control: ComputerUsePluginControl; action: ComputerUsePluginAction };

export function useComputerUseSettings(active: boolean) {
  const { t } = useI18n();
  const connected = useAppStore(state => state.isConnected);
  const conversationId = useAppStore(state => state.conversationId);
  const workingDirectory = useAppStore(state => state.workingDirectory);
  const registryVersion = useAppStore(state => state.runtimeCapabilities?.mcp_registry_version);
  const sessionId = connected ? getWebSocket()?.sessionId : undefined;
  const owner = useMemo<ComputerUseSettingsOwner | null>(() => active && sessionId ? {
    session_id: sessionId, conversation_id: conversationId, workspace_root: workingDirectory || null,
  } : null, [active, sessionId, conversationId, workingDirectory, registryVersion]);
  const current = useRef<Scope | null>(null);
  const [state, setState] = useState<State>({ scope: null, data: null, loading: false, error: "", removing: null, pluginPending: null });
  const view: State = state.scope?.owner === owner && state.scope === current.current ? state
    : { scope: null, data: null, loading: owner !== null, error: "", removing: null, pluginPending: null };

  useEffect(() => {
    if (!owner) { current.current = null; setState({ scope: null, data: null, loading: false, error: "", removing: null, pluginPending: null }); return; }
    const scope: Scope = { owner, lifetime: new AbortController(), request: 0, busy: false, data: null };
    current.current = scope;
    const read = () => {
      if (scope.busy) return;
      const request = ++scope.request;
      setState({ scope, data: scope.data, loading: true, error: "", removing: null, pluginPending: null });
      void readComputerUseSettings(scope.owner, scope.lifetime.signal).then(data => {
        if (current.current !== scope || scope.lifetime.signal.aborted || request !== scope.request) return;
        scope.data = data;
        setState({ scope, data, loading: false, error: "", removing: null, pluginPending: null });
      }, error => {
        if (current.current !== scope || scope.lifetime.signal.aborted || request !== scope.request) return;
        setState({ scope, data: scope.data, loading: false, error: error instanceof Error ? error.message : String(error), removing: null, pluginPending: null });
      });
    };
    read();
    window.addEventListener("focus", read);
    return () => { scope.lifetime.abort(); if (current.current === scope) current.current = null; window.removeEventListener("focus", read); };
  }, [owner]);

  const originalScope = () => {
    const scope = view.scope;
    const snapshot = view.data!;
    if (!scope || current.current !== scope || scope.lifetime.signal.aborted
      || scope.data?.provider_id !== snapshot.provider_id || scope.data.selection_id !== snapshot.selection_id) return null;
    return scope;
  };
  const mutate = async (change: Mutation): Promise<boolean> => {
    const scope = originalScope();
    if (!scope || scope.busy) return false;
    const snapshot = view.data!;
    scope.busy = true;
    ++scope.request;
    setState({ scope, data: scope.data, loading: false, error: "",
      removing: change.kind === "remove" ? change.app.app_id : null,
      pluginPending: change.kind === "plugin" ? change.control.id : null });
    try {
      const data = change.kind === "remove"
        ? await removeComputerUseAppApproval(scope.owner, snapshot, change.app, scope.lifetime.signal)
        : await updateComputerUsePlugin(scope.owner, snapshot, change.control, change.action, scope.lifetime.signal);
      if (current.current !== scope || scope.lifetime.signal.aborted) return false;
      scope.data = data;
      if (change.kind === "plugin") {
        sendClientCommand({ type: "skills.list" }, { silent: true });
        sendClientCommand({ type: "mcp.list" }, { silent: true });
        sendClientCommand({ type: "runtime.capabilities.inspect", source: "settings.computer-use" }, { silent: true });
      }
      const error = data.runtime_refresh?.ok === false
        ? [...data.runtime_refresh.errors ?? [], ...data.runtime_refresh.warnings ?? []].join("；") || t("插件配置已保存，但运行时尚未完成加载。") : "";
      setState({ scope, data, loading: false, error, removing: null, pluginPending: null });
      return error === "";
    } catch (error) {
      if (current.current !== scope || scope.lifetime.signal.aborted) return false;
      setState({ scope, data: scope.data, loading: false, error: error instanceof Error ? error.message : String(error), removing: null, pluginPending: null });
      return false;
    } finally { scope.busy = false; }
  };
  const openPluginDetails = (control: ComputerUsePluginControl) => {
    if (!originalScope()) return;
    useAppStore.getState().toggleSkillsMarketplace("settings", "plugins");
    useAppStore.setState({ skillsMarketplaceSelectedPluginId: control.plugin_id });
  };
  return { ...view, remove: (app: ComputerUseApprovedApp) => mutate({ kind: "remove", app }),
    updatePlugin: (control: ComputerUsePluginControl, action: ComputerUsePluginAction) => mutate({ kind: "plugin", control, action }), openPluginDetails };
}
