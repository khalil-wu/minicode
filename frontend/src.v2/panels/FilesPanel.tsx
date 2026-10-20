import { useI18n } from "../i18n";
import { LoaderCircle } from "../lib/icons";
import { useAppStore } from "../stores";
import { ConversationFilesTree } from "../shell/tabs/ConversationFilesTree";

/** The New Tab Files action opens a file tab owned by the current chat. */
export function FilesPanel({ active = true }: { active?: boolean }) {
  const { t } = useI18n();
  const conversationId = useAppStore(state => state.conversationId);
  const conversation = useAppStore(state => state.conversations.find(item => item.id === state.conversationId));
  const connected = useAppStore(state => state.isConnected);
  const pendingSwitch = useAppStore(state => state.pendingConversationSwitchId);
  const version = useAppStore(state => state.fileTreeVersion);
  const executionCwd = conversation?.executionCwd || conversation?.worktreePath || conversation?.workspaceRoot || "";
  const browserRoot = conversation?.projectlessBrowserRoot || executionCwd;

  return <div className="mc-files-panel" style={{ height: "100%", minHeight: 0, overflow: "auto", paddingBlock: 8 }}>
    {pendingSwitch ? <div role="status" aria-label={t("正在加载文件…")} style={{ display: "grid", placeItems: "center", minHeight: 160 }}><LoaderCircle size={18} className="animate-spin" /></div>
      : !conversationId || !executionCwd ? <div style={{ padding: 24, color: "var(--text-secondary)", fontFamily: "var(--font-ui)" }}>
        <p>{t("文件不可用")}</p><p>{t("聊天拥有环境后即可查看文件。")}</p>
      </div> : null}
    {conversationId && executionCwd && <div hidden={Boolean(pendingSwitch)}>
      <ConversationFilesTree key={`${conversationId}:${executionCwd}:${browserRoot}`}
        owner={{ conversationId, executionCwd }} browserRoot={browserRoot}
        connected={active && connected && !pendingSwitch} version={version} />
    </div>}
  </div>;
}
