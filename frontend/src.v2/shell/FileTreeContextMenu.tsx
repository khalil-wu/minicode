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
import { isDesktop, desktop, revealPath } from "../desktop/runtime";
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
  const copyPath = () => {
    navigator.clipboard.writeText(menu.path);
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
    const name = await showPrompt({ title: "新建文件", message: "文件名：", placeholder: "example.ts" });
    if (!name) return;
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      await writeWorkspaceFile(targetPath, "", workingDirectory);
      onRefresh();
    } catch (error) {
      await showAlert({ title: "创建失败", message: error instanceof Error ? error.message : String(error) });
    }
  };

  const createChildFolder = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: "新建文件夹", message: "文件夹名：", placeholder: "components" });
    if (!name) return;
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      await createWorkspaceDirectory(targetPath, workingDirectory);
      onRefresh();
    } catch (error) {
      await showAlert({ title: "创建失败", message: error instanceof Error ? error.message : String(error) });
    }
  };

  const deleteFile = async () => {
    const { showConfirm, showAlert } = await import("../overlays/DialogService");
    const ok = await showConfirm({
      title: "删除",
      message: `删除 ${menu.path}？`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      if (isDesktop()) {
        let result = await desktop()?.fs.deletePath(menu.path, menu.isDir, false);
        if (result && "needsConfirmation" in result && result.needsConfirmation) {
          const confirmed = await showConfirm({
            title: "确认删除大型目录",
            message: `${menu.path} 包含 ${result.entryCount}+ 个项目。将其移到回收站？`,
            confirmLabel: "移到回收站",
            danger: true,
          });
          if (!confirmed) return;
          result = await desktop()?.fs.deletePath(menu.path, menu.isDir, true);
        }
        if (!result || !("deleted" in result) || !result.deleted) throw new Error(`无法删除：${menu.path}`);
      } else {
        await deleteWorkspacePath(menu.path, workingDirectory, menu.isDir);
      }
      onRefresh();
    } catch (error) {
      await showAlert({ title: "删除失败", message: error instanceof Error ? error.message : `无法删除：${menu.path}` });
    }
  };

  const renameFile = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const newName = await showPrompt({
      title: "重命名",
      message: "新名称：",
      defaultValue: menu.path.split(/[/\\]/).pop() ?? "",
    });
    if (!newName) return;
    if (/[/\\]/.test(newName) || newName === ".." || newName.startsWith("../") || newName.startsWith("..\\")) {
      await showAlert({ title: "名称无效", message: "文件名不能包含路径分隔符或遍历模式。" });
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
      await renameWorkspacePath(menu.path, newPath, workingDirectory);
      useAppStore.getState().renameEditorPath(menu.path, newPath, workingDirectory);
      onRefresh();
    } catch (error) {
      await showAlert({ title: "重命名失败", message: error instanceof Error ? error.message : String(error) });
    }
  };

  const revealInExplorer = () => {
    revealPath(menu.path);
  };

  const items = [
    ...(!menu.isDir && isEditableFile(menu.path)
      ? [{ label: "在编辑器中打开", action: openInEditor, icon: <FilePenLine size={14} /> }]
      : []),
    ...(!menu.isDir && isPreviewableFile(menu.path) ? [{ label: "在预览面板中打开", action: openPreview, icon: <Eye size={14} /> }] : []),
    ...(menu.isDir ? [
      { label: "新建文件…", action: createChildFile, icon: <FilePlus2 size={14} /> },
      { label: "新建文件夹…", action: createChildFolder, icon: <FolderPlus size={14} /> },
    ] : []),
    ...(isDesktop() ? [{ label: "在文件资源管理器中显示", action: revealInExplorer, icon: <FolderOpen size={14} /> }] : []),
    { label: "复制路径", action: copyPath, icon: <Copy size={14} /> },
    { label: "重命名…", action: renameFile, icon: <Pencil size={14} /> },
    { label: "删除", action: deleteFile, icon: <Trash2 size={14} />, danger: true },
  ];

  return (
    <ContextMenu
      position={{ x: menu.x, y: menu.y }}
      items={items.map(({ action, ...item }) => ({ ...item, onClick: action }))}
      onClose={onClose}
    />
  );
};
