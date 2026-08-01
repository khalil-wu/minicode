import React from "react";

// ── Types ──────────────────────────────────────────────────────────────

export type GitStatus = "modified" | "added" | "untracked" | "deleted" | "staged";

export type ExplorerDensity = "compact" | "comfortable";

export type FileSearchResult = {
  path: string;
  name: string;
  score?: number;
  kind?: "file" | "folder";
};

export interface ContextMenuState {
  x: number;
  y: number;
  path: string;
  isDir: boolean;
}

// ── Constants ──────────────────────────────────────────────────────────

export const ROW_HEIGHT: Record<ExplorerDensity, number> = {
  compact: 30,
  comfortable: 34,
};

export const HIDDEN_TREE_NAMES = new Set([
  ".git",
  ".playwright-mcp",
  ".pytest_cache",
  ".tmp",
  "test-results",
  "node_modules",
  "__pycache__",
  "dist",
  "build",
]);

export const GIT_STATUS_COLOR: Record<GitStatus, string> = {
  modified: "var(--state-warning, #e5a50a)",
  added: "var(--state-success)",
  untracked: "var(--text-muted)",
  deleted: "var(--state-danger)",
  staged: "var(--state-info)",
};

export const GIT_STATUS_LABEL: Record<GitStatus, string> = {
  modified: "M",
  added: "A",
  untracked: "U",
  deleted: "D",
  staged: "S",
};

// ── Styles ─────────────────────────────────────────────────────────────

export const refreshBtn: React.CSSProperties = {
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  padding: "4px 10px",
  fontSize: "var(--text-xs)",
  color: "var(--text-secondary)",
  cursor: "pointer",
};

export const fileTreeRootStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  display: "flex",
  flexDirection: "column",
  fontSize: "var(--text-sm)",
  background: "var(--surface-sidebar)",
};

export const fileTreeHeaderStyle: React.CSSProperties = {
  minHeight: 42,
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "7px 14px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
};

export const fileTreeRootLabelStyle: React.CSSProperties = {
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 630,
};

export const fileTreeToolbarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "6px 10px 8px",
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
};

export const fileTreeListStyle: React.CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: "auto",
  padding: "8px 0",
};

export const fileTreeSearchStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  height: 32,
  display: "flex",
  alignItems: "center",
  gap: 5,
  padding: "0 9px",
  border: "1px solid var(--border-subtle)",
  borderRadius: 9,
  background: "var(--surface-base)",
  color: "var(--text-muted)",
};

export const fileTreeSearchInputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  border: 0,
  outline: 0,
  background: "transparent",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
};

export const toolbarMenuWrapStyle: React.CSSProperties = {
  position: "relative",
  flexShrink: 0,
};

export const toolbarMenuStyle: React.CSSProperties = {
  position: "absolute",
  top: 34,
  right: 0,
  zIndex: 50,
  minWidth: 150,
  padding: 4,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 7px)",
  background: "var(--surface-raised)",
  boxShadow: "var(--shadow-md)",
};

export const toolbarMenuItemStyle: React.CSSProperties = {
  width: "100%",
  height: 30,
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "0 8px",
  border: 0,
  borderRadius: "var(--radius-sm, 5px)",
  background: "transparent",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  textAlign: "left",
};

export const treeChevronStyle: React.CSSProperties = {
  width: 16,
  display: "inline-flex",
  justifyContent: "center",
  color: "var(--text-muted)",
  flexShrink: 0,
};

export const treeIconStyle: React.CSSProperties = {
  width: 17,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
};

export const gitBadgeStyle: React.CSSProperties = {
  minWidth: 16,
  height: 16,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: "var(--text-3xs)",
  fontWeight: 750,
  borderRadius: 4,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  flexShrink: 0,
};

export const folderCountStyle: React.CSSProperties = {
  color: "var(--text-tertiary)",
  fontSize: "var(--text-3xs)",
  flexShrink: 0,
};

export const fileMetaStyle: React.CSSProperties = {
  color: "var(--text-tertiary)",
  fontSize: "var(--text-3xs)",
  flexShrink: 0,
};

export const fileTreeEmptyStyle: React.CSSProperties = {
  margin: 8,
  padding: "12px 10px",
  display: "grid",
  gap: 10,
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.45,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
};

export const openWorkspaceButtonStyle: React.CSSProperties = {
  justifySelf: "start",
  height: 28,
  padding: "0 10px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 650,
};

export const searchResultRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minHeight: 40,
  margin: "2px 8px",
  padding: "5px 9px",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
};

export const searchResultNameStyle: React.CSSProperties = {
  display: "block",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  lineHeight: "20px",
};

export const searchResultPathStyle: React.CSSProperties = {
  display: "block",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  fontSize: "var(--text-2xs)",
  lineHeight: "14px",
};
