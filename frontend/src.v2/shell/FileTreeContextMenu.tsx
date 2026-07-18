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
  previewUrlForPath,
  joinWorkspacePath,
} from "./fileTreeHelpers";

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
    useAppStore.getState().setPreviewArtifact({
      artifactId: menu.path,
      content: "",
      name,
      mediaType: mediaTypeForPath(menu.path),
      url: previewUrlForPath(isDesktop() ? joinWorkspacePath(workingDirectory, menu.path) : menu.path),
      loadedAt: Date.now(),
    });
    useAppStore.getState().setRightStackTab("preview");
    onClose();
  };

  const createChildFile = async () => {
    const { showPrompt } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: "New file", message: "File name:", placeholder: "example.ts" });
    if (!name) { onClose(); return; }
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (isDesktop()) await desktop()?.fs.writeFile(targetPath, "");
      else await writeWorkspaceFile(targetPath, "");
      onRefresh();
    } finally {
      onClose();
    }
  };

  const createChildFolder = async () => {
    const { showPrompt } = await import("../overlays/DialogService");
    const name = await showPrompt({ title: "New folder", message: "Folder name:", placeholder: "components" });
    if (!name) { onClose(); return; }
    const base = menu.path === "." ? "" : menu.path.replace(/[\\/]+$/, "");
    const path = base ? `${base}/${name}` : name;
    const targetPath = isDesktop() ? joinWorkspacePath(workingDirectory, path) : path;
    try {
      if (isDesktop()) await desktop()?.fs.createDirectory(targetPath);
      else await createWorkspaceDirectory(targetPath);
      onRefresh();
    } finally {
      onClose();
    }
  };

  const deleteFile = async () => {
    const { showConfirm } = await import("../overlays/DialogService");
    const ok = await showConfirm({
      title: "Delete",
      message: `Delete ${menu.path}?`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) { onClose(); return; }
    if (isDesktop()) {
      await desktop()?.fs.deletePath(menu.path, menu.isDir);
    } else {
      await deleteWorkspacePath(menu.path, menu.isDir);
    }
    onRefresh();
    onClose();
  };

  const renameFile = async () => {
    const { showPrompt, showAlert } = await import("../overlays/DialogService");
    const newName = await showPrompt({
      title: "Rename",
      message: "New name:",
      defaultValue: menu.path.split(/[/\\]/).pop() ?? "",
    });
    if (!newName) { onClose(); return; }
    if (/[/\\]/.test(newName) || newName === ".." || newName.startsWith("../") || newName.startsWith("..\\")) {
      await showAlert({ title: "Invalid name", message: "File names cannot contain path separators or traversal patterns." });
      onClose();
      return;
    }
    const parent = menu.path.replace(/[/\\][^/\\]+$/, "");
    const newPath = parent ? `${parent}/${newName}` : newName;
    if (isDesktop()) {
      await desktop()?.fs.renamePath(menu.path, newPath);
    } else {
      await renameWorkspacePath(menu.path, newPath);
    }
    onRefresh();
    onClose();
  };

  const revealInExplorer = () => {
    revealPath(menu.path);
    onClose();
  };

  const items = [
    ...(!menu.isDir ? [{ label: "Open in Editor", action: openInEditor, icon: <FilePenLine size={14} /> }] : []),
    ...(!menu.isDir && isPreviewableFile(menu.path) ? [{ label: "Open in Preview Pane", action: openPreview, icon: <Eye size={14} /> }] : []),
    ...(menu.isDir ? [
      { label: "New File...", action: createChildFile, icon: <FilePlus2 size={14} /> },
      { label: "New Folder...", action: createChildFolder, icon: <FolderPlus size={14} /> },
    ] : []),
    ...(isDesktop() ? [{ label: "Reveal in Explorer", action: revealInExplorer, icon: <FolderOpen size={14} /> }] : []),
    { label: "Copy Path", action: copyPath, icon: <Copy size={14} /> },
    { label: "Rename...", action: renameFile, icon: <Pencil size={14} /> },
    { label: "Delete", action: deleteFile, icon: <Trash2 size={14} />, danger: true },
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
