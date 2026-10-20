import { desktop, getDesktopPreferences, isDesktop, openExternal, type DesktopPreferences, type FileOpenTargetId, type OpenPreferredPathOptions } from "../desktop/runtime";
import { openWebInBrowser } from "../chat/openWebInBrowser";
import { openWorkspaceSourceFile } from "../workspace/openWorkspaceFolder";
import { normalizeWorkspacePath, workspaceRootsEqual } from "./workspace-path";
import { pushToast } from "../overlays/ToastContainer";
import { useAppStore } from "../stores";
import nativeFileExtensions from "../../../desktop/native-open-file-extensions.json";
import { matchesExternalBrowserClick, type LinkClickModifiers } from "./shortcut-navigation";

export function isNativeExternalFile(path: string) {
  const name = path.split(/[\\/]/).at(-1)!;
  const separator = name.lastIndexOf(".");
  return separator > 0 && nativeFileExtensions.includes(name.slice(separator + 1).toLowerCase());
}

export function preferredFileTargetForWorkspace(preferences: DesktopPreferences, workspaceRoot: string) {
  return Object.entries(preferences.fileOpenTargetPerPath).find(([path]) => workspaceRootsEqual(path, workspaceRoot))?.[1] ?? preferences.fileOpenTarget;
}

export async function openPreferredFile(path: string, workspaceRoot?: string, line?: number, onOpenInApp?: () => void, options: OpenPreferredPathOptions = {}) {
  if (workspaceRoot === "") return;
  if (!isDesktop()) { openWorkspaceSourceFile(path, workspaceRoot, line); return; }
  const normalized = normalizeWorkspacePath(path);
  const root = workspaceRoot ?? useAppStore.getState().workingDirectory;
  const absolutePath = normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized) ? normalized : `${normalizeWorkspacePath(root)}/${normalized}`;
  try {
    const result = await desktop()!.openPreferredPath(absolutePath, line, { workspaceRoot: root, ...options });
    if (result.openInEditor) {
      if (onOpenInApp) onOpenInApp();
      else openWorkspaceSourceFile(result.path, workspaceRoot, line);
    }
  } catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
}

export async function openPreferredWorkspace(workspaceRoot: string, targetId: FileOpenTargetId) {
  try {
    await desktop()!.openPreferredPath(workspaceRoot, undefined, {
      targetId, workspaceRoot, persistPreferredTargetPath: workspaceRoot,
    });
  } catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); }
}

export async function openPreferredWebLink(url: string, conversationId?: string, click?: LinkClickModifiers): Promise<boolean> {
  if (!isDesktop()) { window.open(url, "_blank", "noopener,noreferrer"); return true; }
  try {
    const parsed = new URL(url);
    const preferences = await getDesktopPreferences();
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
    const target = click && matchesExternalBrowserClick(click, preferences.externalBrowserLinkShortcut)
      ? "external-browser" : local ? preferences.localUrlOpenTarget : preferences.urlOpenTarget;
    if (conversationId !== undefined && (useAppStore.getState().conversationId !== conversationId || useAppStore.getState().pendingConversationSwitchId)) {
      throw new Error("会话已切换，请回到原请求后打开链接。");
    }
    if (target === "external-browser") await openExternal(url);
    else if (!openWebInBrowser(url)) throw new Error("请先选择一个对话，再打开内置浏览器页面。");
    return true;
  } catch (error) { pushToast(error instanceof Error ? error.message : String(error), "error"); return false; }
}
