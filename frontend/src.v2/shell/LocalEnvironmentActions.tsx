import { useEffect, useState } from "react";
import { Bug, FlaskConical, Play, Settings2 } from "../lib/icons";
import { useAppStore } from "../stores";
import { workspaceRequestOwner } from "../protocol/workspace-owner";
import { activeLocalEnvironment, type ActiveLocalEnvironment } from "../protocol/local-environments";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import "./LocalEnvironmentActions.css";

const icons = { tool: Settings2, run: Play, debug: Bug, test: FlaskConical };

export function LocalEnvironmentActions() {
  const connected = useAppStore(state => state.isConnected);
  const conversationId = useAppStore(state => state.conversationId);
  const pending = useAppStore(state => state.pendingConversationSwitchId);
  const root = useAppStore(state => state.workingDirectory);
  const registration = useAppStore(state => state.runtimeSession);
  const [revision, setRevision] = useState(0);
  const [loaded, setLoaded] = useState<{ owner: string; session: string; root: string; data: ActiveLocalEnvironment } | null>(null);
  const owner = workspaceRequestOwner();
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    window.addEventListener("local-environments:changed", refresh);
    window.addEventListener("focus", refresh);
    return () => { window.removeEventListener("local-environments:changed", refresh); window.removeEventListener("focus", refresh); };
  }, []);
  useEffect(() => {
    if (!owner || !connected || !conversationId || !root || pending || owner.conversation_id !== conversationId) return;
    const controller = new AbortController();
    void activeLocalEnvironment(root, controller.signal).then(data => { if (!controller.signal.aborted) setLoaded({ owner: conversationId, session: owner.session_id, root, data }); }).catch(reason => { if (!controller.signal.aborted) pushToast(String(reason), "error"); });
    return () => controller.abort();
  }, [connected, conversationId, pending, root, registration, revision, owner?.session_id]);
  if (!loaded || !owner || !connected || pending || owner.conversation_id !== conversationId || loaded.session !== owner.session_id || loaded.owner !== conversationId || loaded.root !== root || loaded.data.actions.length === 0) return null;
  return <>{loaded.data.actions.map(action => {
    const Icon = icons[action.icon || "tool"];
    return <button type="button" className="btn-ghost mc-local-environment-action" key={action.index} title={action.command} onClick={() => {
      const sent = sendClientCommand({ type: "terminal.exec", command: action.command, cwd: loaded.root, workspace_root: loaded.root, conversation_id: loaded.owner,
        local_environment_action: { config_path: loaded.data.config_path, revision: loaded.data.revision, index: action.index } });
      if (sent) useAppStore.getState().openTerminal();
    }}><Icon size={15} /><span>{action.name}</span></button>;
  })}</>;
}
