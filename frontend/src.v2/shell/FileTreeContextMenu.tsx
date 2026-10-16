import { useI18n } from "../i18n";
import { workspaceHasConversation } from "../protocol/workspace-owner";
import { ContextMenu } from "../components/ContextMenu";
import {
  Copy,
  Eye,
  FilePenLine,
  FilePlus2,
  FolderOpen,
  FolderPlus,
  Pencil,
  Trash2,
} from "lucide-react";
import type { WorkspaceTreeNode } from "../protocol/workspace";
import { useAppStore } from "../stores";
import { reconcileRestoredEditorTabs, restoreWorkspaceEditorDrafts } from "../stores/shared-helpers";
import { isDesktop, desktop, revealPath } from "../desktop/runtime";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { pushToast } from "../overlays/ToastContainer";
import { copyText } from "../lib/clipboard";
import { openWorkspaceFilePreview } from "../chat/openAttachmentPreview";
import {
  createWorkspaceDirectory,
  deleteWorkspacePath,
  renameWorkspacePath,
  writeWorkspaceFile,
} from "../protocol/workspace";
import {
  type ContextMenuState,
} from "./fileTreeTypes";
import {
  mediaTypeForPath,
  isPreviewableFile,
  joinWorkspacePath,
  parentTreePath,
  normalizeChangePath,
} from "./fileTreeHelpers";

const editableFilePattern = /\.(?:c|cc|cpp|cs|css|go|h|hpp|html?|java|js|jsx|json|kt|md|mjs|py|rb|rs|scss|sh|sql|swift|toml|ts|tsx|vue|yaml|yml|txt)$/i;

const isEditableFile = (path: string): boolean => editableFilePattern.test(path);

export const FileContextMenu = ({
  menu,
  workingDirectory,
  onRefresh,
  onClose,
}: {
  menu: ContextMenuState;
  workingDirectory: string;
  onRefresh: () => void;
  onClose: () => void;
}) => {
  const { t: tr } = useI18n();
  // Dialogs can outlive the menu. Check the captured scope once at the UI action
  // boundary, before dispatching a mutation; workspace helpers keep that root.
  const continueMenuAction = () => {
    if (workspaceRootsEqual(workingDirectory, useAppStore.getState().workingDirectory)) return true;
    pushToast(tr("工作区已切换，原文件操作已取消；请在当前工作区重新选择。"), "warning");
    return false;
  };
  const copyPath = () => {
    void copyText(menu.path, tr("路径"), tr("已复制路径"));
  };

  const openInEditor = () => {
    useAppStore.getState().openEditorFile(menu.path, menu.path.split(/[/\\]/).pop() ?? menu.path, { exact: true });
  };

  const openPreview = () => {
    const name = menu.path.split(/[/\\]/).pop() ?? menu.path;
    openWorkspaceFilePreview({
      path: menu.path,
      name,
      mediaType: mediaTypeForPath(menu.path),
      workspaceRoot: workingDirectory,
    });
  };

  const createChildFile = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: tr("新建文件"), message: tr("文件名："), placeholder: "example.ts" });
    if (!name) return;
    if (!continueMenuAction()) return;
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      await writeWorkspaceFile(targetPath, "", workingDirectory);
      onRefresh();
    } catch (error) {
      await showAlert({ title: tr("创建失败"), message: error instanceof Error ? error.message : String(error) });
    }
  };

  const createChildFolder = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: tr("新建文件夹"), message: tr("文件夹名："), placeholder: "components" });
    if (!name) return;
    if (!continueMenuAction()) return;
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      await createWorkspaceDirectory(targetPath, workingDirectory);
      onRefresh();
    } catch (error) {
      await showAlert({ title: tr("创建失败"), message: error instanceof Error ? error.message : String(error) });
    }
  };

  const deleteFile = async () => {
    const { showConfirm, showAlert } = await import("../overlays/DialogService");
    const ok = await showConfirm({
      title: tr("删除"),
      message: tr("删除 {v0}？", { v0: menu.path }),
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    if (!continueMenuAction()) return;
    try {
      if (isDesktop() && !workspaceHasConversation()) {
        const targetPath = joinWorkspacePath(workingDirectory, menu.path);
        let result = await desktop()?.fs.deletePath(targetPath, menu.isDir, false);
        if (result && "needsConfirmation" in result && result.needsConfirmation) {
          const confirmed = await showConfirm({
            title: tr("确认删除大型目录"),
            message: tr("{v0} 包含 {v1}+ 个项目。将其移到回收站？", { v0: menu.path, v1: result.entryCount }),
            confirmLabel: tr("移到回收站"),
            danger: true,
          });
          if (!confirmed) return;
          if (!continueMenuAction()) return;
          result = await desktop()?.fs.deletePath(targetPath, menu.isDir, true);
        }
        if (!result || !("deleted" in result) || !result.deleted) throw new Error(tr("无法删除：{v0}", { v0: menu.path }));
      } else {
        await deleteWorkspacePath(menu.path, workingDirectory, menu.isDir);
      }
      onRefresh();
    } catch (error) {
      await showAlert({ title: tr("删除失败"), message: error instanceof Error ? error.message : tr("无法删除：{v0}", { v0: menu.path }) });
    }
  };

  const renameFile = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const newName = await showPrompt({
      title: tr("重命名"),
      message: tr("新名称："),
      defaultValue: menu.path.split(/[/\\]/).pop() ?? "",
    });
    if (!newName) return;
    if (!continueMenuAction()) return;
    if (/[/\\]/.test(newName) || newName === ".." || newName.startsWith("../") || newName.startsWith("..\\")) {
      await showAlert({ title: tr("名称无效"), message: tr("文件名不能包含路径分隔符或遍历模式。") });
        return;
    }
    // Renaming the workspace root itself keeps the root path (web mode
    // resolves relative paths against the workspace); use the shared
    // parentTreePath helper instead of a bare regex that mangles it.
    const parent = parentTreePath(menu.path, workingDirectory);
    const isRoot = normalizeChangePath(menu.path) === normalizeChangePath(workingDirectory || ".");
    const newPath = isRoot
      ? menu.path
      : `${parent && parent !== "." ? `${parent}/` : ""}${newName}`;
    try {
      const restored = await restoreWorkspaceEditorDrafts(workingDirectory);
      if (!continueMenuAction()) return;
      useAppStore.setState((state) => ({
        editorTabs: reconcileRestoredEditorTabs(state.editorTabs, restored, workingDirectory),
      }));
      await renameWorkspacePath(menu.path, newPath, workingDirectory);
      useAppStore.getState().renameEditorPath(menu.path, newPath, workingDirectory);
      onRefresh();
    } catch (error) {
      await showAlert({ title: tr("重命名失败"), message: error instanceof Error ? error.message : String(error) });
    }
  };

  const revealInExplorer = () => {
    revealPath(menu.path);
  };

  const items = [
    ...(!menu.isDir && isEditableFile(menu.path)
      ? [{ label: tr("在编辑器中打开"), action: openInEditor, icon: <FilePenLine size={14} /> }]
      : []),
    ...(!menu.isDir && isPreviewableFile(menu.path) ? [{ label: tr("在预览面板中打开"), action: openPreview, icon: <Eye size={14} /> }] : []),
    ...(menu.isDir ? [
      { label: tr("新建文件…"), action: createChildFile, icon: <FilePlus2 size={14} /> },
      { label: tr("新建文件夹…"), action: createChildFolder, icon: <FolderPlus size={14} /> },
    ] : []),
    ...(isDesktop() ? [{ label: tr("在文件资源管理器中显示"), action: revealInExplorer, icon: <FolderOpen size={14} /> }] : []),
    { label: tr("复制路径"), action: copyPath, icon: <Copy size={14} /> },
    { label: tr("重命名…"), action: renameFile, icon: <Pencil size={14} /> },
    { label: tr("删除"), action: deleteFile, icon: <Trash2 size={14} />, danger: true },
  ];

  return (
    <ContextMenu
      position={{ x: menu.x, y: menu.y }}
      items={items.map(({ action, ...item }) => ({ ...item, onClick: action }))}
      onClose={onClose}
    />
  );
};
