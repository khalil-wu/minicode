import { useEffect } from "react";
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
  useEffect(() => {
    const handler = () => onClose();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("click", handler);
    document.addEventListener("contextmenu", handler);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("click", handler);
      document.removeEventListener("contextmenu", handler);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [onClose]);

  const copyPath = () => {
    navigator.clipboard.writeText(menu.path);
    onClose();
  };

  const openInEditor = () => {
    useAppStore.getState().openEditorFile(menu.path, menu.path.split(/[/\\]/).pop() ?? menu.path);
    onClose();
  };

  const openPreview = () => {
    const name = menu.path.split(/[/\\]/).pop() ?? menu.path;
    openWorkspaceFilePreview({
      path: menu.path,
      name,
      mediaType: mediaTypeForPath(menu.path),
      workspaceRoot: workingDirectory,
    });
    onClose();
  };

  const createChildFile = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: "新建文件", message: "文件名：", placeholder: "example.ts" });
    if (!name) { onClose(); return; }
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (!(await writeWorkspaceFile(targetPath, "", workingDirectory))) {
        await showAlert({ title: "创建失败", message: `无法创建文件：${path}。目标可能已存在或不可写。` });
        return;
      }
      onRefresh();
    } finally {
      onClose();
    }
  };

  const createChildFolder = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: "新建文件夹", message: "文件夹名：", placeholder: "components" });
    if (!name) { onClose(); return; }
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (!(await createWorkspaceDirectory(targetPath, workingDirectory))) {
        await showAlert({ title: "创建失败", message: `无法创建文件夹：${path}。目标可能已存在或不可写。` });
        return;
      }
      onRefresh();
    } finally {
      onClose();
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
    if (!ok) { onClose(); return; }
    if (isDesktop()) {
      const result = await desktop()?.fs.deletePath(menu.path, menu.isDir, false);
      if (result && "needsConfirmation" in result && result.needsConfirmation) {
        const confirmed = await showConfirm({
          title: "确认删除大型目录",
          message: `${menu.path} 包含 ${result.entryCount}+ 个项目。将其移到回收站？`,
          confirmLabel: "移到回收站",
          danger: true,
        });
        if (!confirmed) { onClose(); return; }
        const finalResult = await desktop()?.fs.deletePath(menu.path, menu.isDir, true);
        if (!finalResult || !("deleted" in finalResult) || !finalResult.deleted) {
          onClose();
          return;
        }
      } else if (!result || !("deleted" in result) || !result.deleted) {
        onClose();
        return;
      }
    } else {
      if (!(await deleteWorkspacePath(menu.path, workingDirectory, menu.isDir))) {
        await showAlert({ title: "删除失败", message: `无法删除：${menu.path}` });
        onClose();
        return;
      }
    }
    onRefresh();
    onClose();
  };

  const renameFile = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const newName = await showPrompt({
      title: "重命名",
      message: "新名称：",
      defaultValue: menu.path.split(/[/\\]/).pop() ?? "",
    });
    if (!newName) { onClose(); return; }
    if (/[/\\]/.test(newName) || newName === ".." || newName.startsWith("../") || newName.startsWith("..\\")) {
      await showAlert({ title: "名称无效", message: "文件名不能包含路径分隔符或遍历模式。" });
      onClose();
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
    if (!(await renameWorkspacePath(menu.path, newPath, workingDirectory))) {
      await showAlert({ title: "重命名失败", message: `无法将 ${menu.path} 重命名为 ${newPath}。目标可能已存在或不可写。` });
      onClose();
      return;
    }
    onRefresh();
    onClose();
  };

  const revealInExplorer = () => {
    revealPath(menu.path);
    onClose();
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
    <div
      style={{
        position: "fixed",
        left: menu.x,
        top: menu.y,
        background: "var(--surface-raised)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-sm, 6px)",
        boxShadow: "var(--shadow-md)",
        padding: 4,
        zIndex: "var(--z-context-menu)",
        minWidth: 184,
      }}
    >
      {items.map((item) => (
        <button
          key={item.label}
          type="button"
          className={item.danger ? "btn-ghost-danger" : "btn-ghost"}
          onClick={item.action}
          style={{
            minHeight: 30,
            display: "flex",
            alignItems: "center",
            gap: 8,
            width: "100%",
            textAlign: "left",
            border: 0,
            padding: "0 8px",
            fontSize: "var(--text-xs)",
            color: item.danger ? "var(--state-danger)" : "var(--text-secondary)",
            cursor: "pointer",
            borderRadius: "var(--radius-sm, 4px)",
          }}
        >
          <span aria-hidden="true" style={{ width: 16, display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
            {item.icon}
          </span>
          <span>{item.label}</span>
        </button>
      ))}
    </div>
  );
};
