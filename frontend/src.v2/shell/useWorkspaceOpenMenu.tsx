import { useEffect, useState } from "react";
import type { ContextMenuItem } from "../components/ContextMenu";
import { FileOpenTargetIcon } from "../components/FileOpenTargetIcon";
import { getDesktopPreferenceTargets, isDesktop, type FileOpenTarget } from "../desktop/runtime";
import { useI18n } from "../i18n";
import { openPreferredWorkspace } from "../lib/open-preferred-target";
import { useAppStore } from "../stores";
import { fetchWorkspaceGitStatus } from "../protocol/workspace";

/** Source OJo/kJo share the workspace target catalog between sidebar and header. */
export function useWorkspaceOpenMenu(workspaceRoot: string | undefined, opened: boolean): ContextMenuItem[] {
  const { t: tr } = useI18n();
  const appMode = useAppStore(state => state.appMode);
  const [discovery, setDiscovery] = useState<{ workspaceRoot?: string; gitBacked: boolean; targets: FileOpenTarget[] | null; failed: boolean }>({ gitBacked: false, targets: null, failed: false });
  useEffect(() => {
    if (!opened || !workspaceRoot || !isDesktop()) return;
    let current = true;
    let gitBacked = false;
    setDiscovery({ workspaceRoot, gitBacked, targets: null, failed: false });
    void (async () => {
      const status = await fetchWorkspaceGitStatus(workspaceRoot);
      if (!current) return;
      gitBacked = status.is_git_repo === true;
      setDiscovery({ workspaceRoot, gitBacked, targets: null, failed: false });
      if (!gitBacked) return;
      const targets = await getDesktopPreferenceTargets();
      if (current) setDiscovery({ workspaceRoot, gitBacked, targets: targets.fileOpenTargets, failed: false });
    })().catch(() => {
      if (current) setDiscovery({ workspaceRoot, gitBacked, targets: null, failed: true });
    });
    return () => { current = false; };
  }, [opened, workspaceRoot]);
  if (!opened || !workspaceRoot || !isDesktop() || discovery.workspaceRoot !== workspaceRoot || !discovery.gitBacked) return [];
  const targets = (discovery.workspaceRoot === workspaceRoot ? discovery.targets : null)?.filter(target => !target.hidden && (appMode !== "cowork" || target.kind !== "editor" && target.kind !== "terminal"));
  if (targets?.length === 0) return [];
  return [{ label: tr("打开方式"), icon: <span aria-hidden="true" style={{ width: 16, height: 16, background: "currentColor", mask: `url(${import.meta.env.BASE_URL}apps/open-in.svg) center / contain no-repeat` }} />,
    submenu: targets?.map(target => ({ label: target.label, icon: <FileOpenTargetIcon target={target} />,
      onClick: () => void openPreferredWorkspace(workspaceRoot, target.id) }))
      ?? [{ label: discovery.failed ? tr("无法加载应用。请重新打开菜单以重试") : tr("正在加载可用应用…"), disabled: true }],
  }];
}
