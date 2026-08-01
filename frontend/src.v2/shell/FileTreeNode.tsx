import { memo, type MouseEvent as ReactMouseEvent } from "react";
import { ChevronDown, ChevronRight, LoaderCircle } from "lucide-react";
import type { WorkspaceTreeNode } from "../protocol/workspace";
import { useAppStore } from "../stores";
import {
  type GitStatus,
  type ExplorerDensity,
  type ContextMenuState,
  ROW_HEIGHT,
  GIT_STATUS_COLOR,
  GIT_STATUS_LABEL,
  treeChevronStyle,
  treeIconStyle,
  gitBadgeStyle,
  folderCountStyle,
  fileMetaStyle,
} from "./fileTreeTypes";
import {
  nodeMatchesQuery,
  filteredChildren,
  isSameTreePath,
  formatFileMeta,
  formatBytes,
  iconColor,
  fileIcon,
  folderIcon,
} from "./fileTreeHelpers";

export const TreeNode = memo(({
  node,
  depth,
  gitMap,
  loadingPaths,
  expandedPaths,
  query,
  workingDirectory,
  activeEditorPath,
  density,
  onToggleExpanded,
  onContextMenu,
  onNavigate,
}: {
  node: WorkspaceTreeNode;
  depth: number;
  gitMap: Map<string, GitStatus>;
  loadingPaths: Set<string>;
  expandedPaths: Set<string>;
  query: string;
  workingDirectory: string;
  activeEditorPath: string | null;
  density: ExplorerDensity;
  onToggleExpanded: (path: string, shouldLoad?: boolean) => void;
  onContextMenu: (menu: ContextMenuState) => void;
  onNavigate?: () => void;
}) => {
  const hasQuery = query.trim().length > 0;
  const expanded = expandedPaths.has(node.path) || (hasQuery && nodeMatchesQuery(node, query));
  const loading = loadingPaths.has(node.path);
  const selected = !node.is_dir && isSameTreePath(activeEditorPath, node.path);
  const childCount = node.is_dir ? filteredChildren(node, query).length : 0;
  const gitStatus = gitMap.get(node.path);
  const toggle = () => {
    if (node.is_dir) onToggleExpanded(node.path, !expanded && (node.children?.length ?? 0) === 0);
  };

  const openFile = () => {
    if (!node.is_dir) {
      useAppStore.getState().openEditorFile(node.path, node.name);
      onNavigate?.();
    }
  };

  const handleContextMenu = (e: ReactMouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    onContextMenu({ x: e.clientX, y: e.clientY, path: node.path, isDir: node.is_dir });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      node.is_dir ? toggle() : openFile();
    } else if (e.key === "ArrowRight" && node.is_dir && !expanded) {
      e.preventDefault();
      onToggleExpanded(node.path, (node.children?.length ?? 0) === 0);
    } else if (e.key === "ArrowLeft" && node.is_dir && expanded) {
      e.preventDefault();
      onToggleExpanded(node.path, false);
    }
  };

  return (
    <div>
      <div
        role="treeitem"
        tabIndex={0}
        className="tree-node-hover"
        data-selected={selected || undefined}
        aria-expanded={node.is_dir ? expanded : undefined}
        aria-selected={selected}
        data-tree-path={node.path}
        onClick={node.is_dir ? toggle : openFile}
        onKeyDown={handleKeyDown}
        onContextMenu={handleContextMenu}
        title={formatFileMeta(node)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          height: ROW_HEIGHT[density],
          margin: "1px 8px",
          padding: "0 8px",
          paddingLeft: 8 + depth * 15,
          cursor: "pointer",
          color: selected ? "var(--text-primary)" : node.is_dir ? "var(--text-primary)" : "var(--text-secondary)",
          background: selected ? "var(--surface-active)" : "transparent",
          border: "1px solid transparent",
          borderColor: selected ? "color-mix(in oklch, var(--accent-primary) 32%, transparent)" : "transparent",
          borderRadius: "var(--radius-sm, 6px)",
          boxShadow: selected ? "inset 2px 0 0 var(--accent-primary)" : "none",
          transition: "background 80ms ease, border-color 80ms ease, box-shadow 80ms ease",
        }}
      >
        <span className="file-tree-chevron" style={treeChevronStyle} aria-hidden="true">
          {node.is_dir ? (
            expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />
          ) : (
            <span style={{ width: 14 }} />
          )}
        </span>
        <span style={{ ...treeIconStyle, color: iconColor(node) }} aria-hidden="true">
          {node.is_dir
            ? expanded
              ? folderIcon(true)
              : folderIcon(false)
            : fileIcon(node.name)}
        </span>
        <span style={{
          flex: 1,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          fontSize: "var(--text-sm)",
          lineHeight: "20px",
          fontFamily: "var(--font-ui)",
          color: gitStatus ? GIT_STATUS_COLOR[gitStatus] : undefined,
          fontWeight: selected ? 650 : node.is_dir ? 560 : 450,
        }}>
          {node.name}
        </span>
        {node.is_dir && childCount > 0 && density === "comfortable" && (
          <span style={folderCountStyle}>{childCount}</span>
        )}
        {typeof node.size_bytes === "number" && !node.is_dir && density === "comfortable" && !gitStatus && (
          <span style={fileMetaStyle}>{formatBytes(node.size_bytes)}</span>
        )}
        {gitStatus && (
          <span style={{ ...gitBadgeStyle, color: GIT_STATUS_COLOR[gitStatus] }}>
            {GIT_STATUS_LABEL[gitStatus]}
          </span>
        )}
        {loading && (
          <span role="status" aria-label={`Loading ${node.name}`} style={{ color: "var(--text-muted)", flexShrink: 0, display: "inline-flex" }}>
            <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
          </span>
        )}
      </div>
      {expanded && node.children && (
        <div style={{ position: "relative", marginLeft: depth > 0 ? 0 : 0 }}>
          {depth >= 0 && (
            <span style={{
              position: "absolute",
              left: 21 + depth * 15,
              top: 0,
              bottom: 0,
              width: 1,
              background: "var(--border-subtle)",
              opacity: 0.35,
              pointerEvents: "none",
            }} />
          )}
          {filteredChildren(node, query).map((child) => (
            <TreeNode
              key={child.path}
              node={child}
              depth={depth + 1}
              gitMap={gitMap}
              loadingPaths={loadingPaths}
              expandedPaths={expandedPaths}
              query={query}
              workingDirectory={workingDirectory}
              activeEditorPath={activeEditorPath}
              density={density}
              onToggleExpanded={onToggleExpanded}
              onContextMenu={onContextMenu}
              onNavigate={onNavigate}
            />
          ))}
        </div>
      )}
    </div>
  );
});
