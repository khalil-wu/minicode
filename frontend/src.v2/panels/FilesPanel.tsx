import { useI18n } from "../i18n";
import { LoaderCircle } from "../lib/icons";
import { useAppStore } from "../stores";
import { ConversationFilesTree } from "../shell/tabs/ConversationFilesTree";
import { currentFileWorkspaceOwner, fileWorkspaceScope } from "../protocol/file-workspace";
import { useMemo } from "react";
import { workspacePathWithin } from "../lib/workspace-path";

/** Home uses its selected directory; chat files keep their fixed execution owner. */
export function FilesPanel({ active = true }: { active?: boolean }) {
  const { t } = useI18n();
  const conversationId = useAppStore(state => state.conversationId);
  const conversation = useAppStore(state => state.conversations.find(item => item.id === state.conversationId));
  const connected = useAppStore(state => state.isConnected);
  const pendingSwitch = useAppStore(state => state.pendingConversationSwitchId);
  const version = useAppStore(state => state.fileTreeVersion);
  const directory = useAppStore(state => state.workingDirectory);
  const selectedRoot = useAppStore(state => state.fileBrowserRoots[fileWorkspaceScope(state)]);
  const owner = useMemo(() => {
    const value = currentFileWorkspaceOwner(useAppStore.getState());
    return selectedRoot && (!conversationId || !workspacePathWithin(selectedRoot, value.browserRoot))
      ? { ...value, conversationId: null, executionCwd: selectedRoot, browserRoot: selectedRoot } : value;
  }, [conversationId, conversation, directory, selectedRoot]);
  const browserRoot = selectedRoot || owner.browserRoot;

  return <div className="mc-files-panel" style={{ height: "100%", minHeight: 0, overflow: "auto", paddingBlock: 8 }}>
    {pendingSwitch ? <div role="status" aria-label={t("正在加载文件…")} style={{ display: "grid", placeItems: "center", minHeight: 160 }}><LoaderCircle size={18} className="animate-spin" /></div>
      : null}
    <div hidden={Boolean(pendingSwitch)}>
      <ConversationFilesTree key={`${owner.scopeId}:${owner.executionCwd}:${browserRoot}`}
        owner={owner} browserRoot={browserRoot} onRootChange={path => useAppStore.getState().setFileBrowserRoot(owner.scopeId, path)}
        connected={active && connected && !pendingSwitch} version={version} />
    </div>
  </div>;
}
