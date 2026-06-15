import { useEffect } from "react";
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
    document.addEventListener("click", handler);
    document.addEventListener("contextmenu", handler);
    return () => {
      document.removeEventListener("click", handler);
      document.removeEventListener("contextmenu", handler);
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
    ...(!menu.isDir ? [{ label: "Open in Editor", action: openInEditor }] : []),
    ...(!menu.isDir && isPreviewableFile(menu.path) ? [{ label: "Open in Preview Pane", action: openPreview }] : []),
    ...(menu.isDir ? [
      { label: "New File...", action: createChildFile },
      { label: "New Folder...", action: createChildFolder },
    ] : []),
    ...(isDesktop() ? [{ label: "Reveal in Explorer", action: revealInExplorer }] : []),
    { label: "Copy Path", action: copyPath },
    { label: "Rename...", action: renameFile },
    { label: "Delete", action: deleteFile },
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
        zIndex: 200,
        minWidth: 140,
      }}
    >
      {items.map((item) => (
        <button
          key={item.label}
          className={item.label === "Delete" ? "btn-ghost-danger" : "btn-ghost"}
          onClick={item.action}
          style={{
            display: "block",
            width: "100%",
            textAlign: "left",
            border: 0,
            padding: "5px 10px",
            fontSize: "var(--text-xs)",
            color: item.label === "Delete" ? "var(--state-danger)" : "var(--text-primary)",
            cursor: "pointer",
            borderRadius: "var(--radius-sm, 4px)",
          }}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
};
