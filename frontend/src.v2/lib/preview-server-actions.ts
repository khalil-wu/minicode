import { useAppStore } from "../stores";
import { ApiError } from "../protocol/api";
import { readWorkspaceFile } from "../protocol/workspace";
import type { ClientCommand, CommandResultEvent, PreviewLaunchConfigInfo, PreviewLaunchProcessInfo } from "../protocol/events";
import { commandResultSucceeded, LONG_COMMAND_RESULT_TIMEOUT_MS, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { workspaceFilePathsEqual, workspaceRootsEqual } from "./workspace-path";

export interface PreviewServiceScope {
  conversation_id: string;
  workspace_root: string;
}

export type PreviewServiceResult = CommandResultEvent & { data?: {
  configs?: PreviewLaunchConfigInfo[];
  running?: PreviewLaunchProcessInfo[];
  process?: PreviewLaunchProcessInfo;
  stopped?: PreviewLaunchProcessInfo[];
  verification?: { url: string; ok: boolean; status_code?: number | null; error?: string; elapsed_ms: number };
} };

export const previewServiceScopeIsCurrent = (scope: PreviewServiceScope): boolean => {
  const state = useAppStore.getState();
  return state.conversationId === scope.conversation_id && workspaceRootsEqual(state.workingDirectory, scope.workspace_root);
};

export const runPreviewServiceCommand = async (command: ClientCommand): Promise<PreviewServiceResult> => {
  const result = await sendClientCommandAwaitResult(command, command.type, { silent: true, timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS });
  if (!commandResultSucceeded(result)) throw new Error(result.message || "预览服务操作失败。");
  return result as PreviewServiceResult;
};

export const restartPreviewService = async (scope: PreviewServiceScope, name: string): Promise<PreviewServiceResult | null> => {
  await runPreviewServiceCommand({ type: "preview.launch.stop", ...scope, name });
  if (!previewServiceScopeIsCurrent(scope)) return null;
  return runPreviewServiceCommand({ type: "preview.launch.start", ...scope, name });
};

export const openPreviewServiceManager = (): void => {
  const state = useAppStore.getState();
  useAppStore.setState({ previewServiceManagerRequest: { conversationId: state.conversationId || "", workspaceRoot: state.workingDirectory } });
  state.setRightStackTab("browser");
};

export const openPreviewLaunchConfiguration = async (scope: PreviewServiceScope, configs: PreviewLaunchConfigInfo[]): Promise<boolean> => {
  if (!previewServiceScopeIsCurrent(scope)) return false;
  const path = `${scope.workspace_root.replace(/[\\/]+$/, "")}/.minicode/launch.json`;
  const state = useAppStore.getState();
  const existing = state.editorTabs.find((tab) => workspaceFilePathsEqual(tab.path, path, scope.workspace_root));
  if (existing && existing.content !== existing.original) {
    state.openEditorFile(path, "launch.json", { exact: true });
    return true;
  }
  let missing = false;
  let file;
  try {
    file = await readWorkspaceFile(".minicode/launch.json", scope.workspace_root);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
    missing = true;
  }
  if (!previewServiceScopeIsCurrent(scope)) return false;
  const current = useAppStore.getState();
  const currentTab = current.editorTabs.find((tab) => workspaceFilePathsEqual(tab.path, path, scope.workspace_root));
  if (currentTab && currentTab.content !== currentTab.original) {
    current.openEditorFile(path, "launch.json", { exact: true });
    return true;
  }
  current.openEditorTab(path);
  if (missing) {
    const content = JSON.stringify({ configurations: configs.map((config) => ({
      name: config.name, command: config.command, cwd: ".", port: config.port,
      ...(config.url ? { url: config.url } : {}), ...(config.auto_port ? { auto_port: true } : {}),
    })) }, null, 2) + "\n";
    current.markTabLoaded(path, "", null, "");
    current.updateTabContent(path, content);
  } else {
    current.markTabLoaded(path, file!.content, null, file!.content_hash, { sizeBytes: file!.size_bytes ?? file!.size });
  }
  current.openEditorFile(path, "launch.json", { exact: true });
  return missing;
};
