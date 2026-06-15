import { Folder } from "lucide-react";
import { useAppStore } from "../stores";
import {
  type GitStatus,
  type FileSearchResult,
  type ContextMenuState,
  GIT_STATUS_COLOR,
  GIT_STATUS_LABEL,
  treeIconStyle,
  gitBadgeStyle,
  searchResultRowStyle,
  searchResultNameStyle,
  searchResultPathStyle,
} from "./fileTreeTypes";
import {
  isSameTreePath,
  iconColor,
  fileIcon,
  workspaceLabel,
} from "./fileTreeHelpers";

export const SearchResultRow = ({
  result,
  gitMap,
  activeEditorPath,
  workingDirectory,
  onContextMenu,
}: {
  result: FileSearchResult;
  gitMap: Map<string, GitStatus>;
  activeEditorPath: string | null;
  workingDirectory: string;
  onContextMenu: (menu: ContextMenuState) => void;
}) => {
  const selected = isSameTreePath(activeEditorPath, result.path);
  const isDir = result.kind === "folder";
  const gitStatus = gitMap.get(result.path);
  const parent = result.path.replace(/\\/g, "/").split("/").slice(0, -1).join("/");
  const openResult = () => {
    if (isDir) return;
    useAppStore.getState().openEditorFile(result.path, result.name);
  };
  return (
    <div
      role="treeitem"
      tabIndex={0}
      aria-selected={selected}
      title={result.path}
      onClick={openResult}
      onKeyDown={(event) => {
        if ((event.key === "Enter" || event.key === " ") && !isDir) {
          event.preventDefault();
          openResult();
        }
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        onContextMenu({ x: event.clientX, y: event.clientY, path: result.path, isDir });
      }}
      style={{
        ...searchResultRowStyle,
        background: selected ? "var(--surface-active)" : "transparent",
        borderColor: selected ? "color-mix(in oklch, var(--accent-primary) 32%, transparent)" : "transparent",
        boxShadow: selected ? "inset 2px 0 0 var(--accent-primary)" : "none",
        cursor: isDir ? "default" : "pointer",
      }}
    >
      <span style={{ ...treeIconStyle, color: isDir ? "var(--accent-primary)" : iconColor({ name: result.name, path: result.path, is_dir: false }) }}>
        {isDir ? <Folder size={14} /> : fileIcon(result.name)}
      </span>
      <span style={{ minWidth: 0, flex: 1 }}>
        <span style={searchResultNameStyle}>{result.name}</span>
        <span style={searchResultPathStyle}>{parent || workspaceLabel(workingDirectory)}</span>
      </span>
      {gitStatus && (
        <span style={{ ...gitBadgeStyle, color: GIT_STATUS_COLOR[gitStatus] }}>
          {GIT_STATUS_LABEL[gitStatus]}
        </span>
      )}
    </div>
  );
};
