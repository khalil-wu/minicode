import React from "react";

export const modeSwitchStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: 3,
  minHeight: 38,
  padding: 3,
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 10px)",
};

export const modeSwitchButtonStyle: React.CSSProperties = {
  height: 30,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 8px)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
  letterSpacing: 0,
};

export const primaryActionGroupStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: 4,
};

export const primaryActionStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 8,  // 🔧 从 6 增加到 8
  height: 36,  // 🔧 从 32 增加到 36
  padding: "0 12px",  // 🔧 从 10px 增加到 12px
  background: "var(--surface-page)",
  color: "var(--text-primary)",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",  // 🔧 保持 12px
  fontWeight: 600,  // 🔧 从 700 降低到 600
};

export const sessionControlStackStyle: React.CSSProperties = {
  display: "grid",
  gap: 12,  // 🔧 从 10 增加到 12
  padding: "16px 12px 12px",  // 🔧 增加 padding
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-sidebar)",
};

export const routineGroupStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,  // 🔧 从 3 增加到 4
  padding: 0,
};

export const sidebarLinkStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,  // 增加间距
  minHeight: 36,  // 增加高度
  padding: "0 12px",  // 增加左右 padding
  background: "transparent",
  color: "var(--text-secondary)",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",  // 🔧 从 text-xs (11px) 改为 text-sm (12px)
  fontWeight: 500,  // 🔧 从 600 降低到 500，更轻盈
  textAlign: "left",
};

export const comingSoonStyle: React.CSSProperties = {
  marginLeft: "auto",
  fontSize: "var(--text-xs)",  // 🔧 从 10px 改为 11px
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
};

export const bulkBarStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  flexWrap: "wrap",
  gap: 6,
  margin: "0 8px 8px",
  padding: "5px 6px",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
};

export const bulkActionStyle: React.CSSProperties = {
  height: 28,
  padding: "0 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-base)",
  color: "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: 600,
  lineHeight: 1,
  whiteSpace: "nowrap",
};

export const bulkMetaStyle: React.CSSProperties = {
  flex: "1 1 auto",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  fontVariantNumeric: "tabular-nums",
  whiteSpace: "nowrap",
};

export const bulkActionsStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  marginLeft: "auto",
};

export const sessionCheckboxStyle: React.CSSProperties = {
  width: 14,
  height: 14,
  flexShrink: 0,
  accentColor: "var(--accent-primary)",
};

export const sessionMainButtonStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: 0,
  background: "transparent",
  border: 0,
  cursor: "pointer",
  textAlign: "left",
};

export const sectionHeaderRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 8,
};

export const sectionMetaStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontSize: "var(--text-2xs)",
  fontFamily: "var(--font-mono)",
};

export const searchBarWrapStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
};

export const searchInputStyle: React.CSSProperties = {
  width: "100%",
  minWidth: 0,
  background: "var(--surface-base)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  height: 30,
  padding: "0 10px",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  outline: "none",
};

export const filterRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  flexWrap: "wrap",
  overflow: "hidden",
};

export const filterButtonStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  height: 24,
  padding: "0 8px",
  border: 0,
  borderRadius: 999,
  cursor: "pointer",
  fontSize: "var(--text-2xs)",
  whiteSpace: "nowrap",
  transition: "var(--transition-fast)",
};

export const filterCountStyle: React.CSSProperties = {
  fontSize: "var(--text-3xs)",
  opacity: 0.7,
  fontFamily: "var(--font-mono)",
};

export const sessionListWrapStyle: React.CSSProperties = {
  flex: 1,
  overflowY: "auto",
  overflowX: "hidden",
  minWidth: 0,
  padding: "2px 0 10px",
  display: "grid",
  alignContent: "start",
  gap: 7,
};

export const emptyStateStyle: React.CSSProperties = {
  padding: 16,
  color: "var(--text-muted)",
  fontSize: "var(--text-sm)",
  textAlign: "center",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 8px)",
};

export const projectGroupStyle: React.CSSProperties = {
  display: "grid",
  gap: 4,
  minWidth: 0,
};

export const projectHeaderStyle: React.CSSProperties = {
  width: "100%",
  display: "flex",
  alignItems: "center",
  gap: 6,
  minHeight: 24,
  padding: "0 6px",
  background: "transparent",
  border: 0,
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  color: "var(--text-muted)",
  fontWeight: 600,
  textAlign: "left",
};

export const projectCountStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)",
  fontWeight: 500,
  marginLeft: "auto",
  opacity: 0.8,
};

export const projectItemsStyle: React.CSSProperties = {
  display: "grid",
  gap: 2,
  minWidth: 0,
};

export const sessionRowStyle: React.CSSProperties = {
  width: "100%",
  minWidth: 0,
  boxSizing: "border-box",
  display: "flex",
  alignItems: "center",
  minHeight: 36,
  padding: "0 8px 0 40px",
  cursor: "pointer",
  gap: 6,
  position: "relative",
  transition: "var(--transition-fast)",
  textAlign: "left",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 6px)",
};

export const sessionTitleStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: "var(--text-sm)",
  fontFamily: "var(--font-prose)",
  fontWeight: 400,
  lineHeight: 1.35,
  color: "var(--text-primary)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

export const sessionMetaLineStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 5,
  minWidth: 0,
  overflow: "hidden",
  whiteSpace: "nowrap",
  fontSize: "var(--text-2xs)",
  color: "var(--text-muted)",
  marginTop: 2,
};

export const branchMetaStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 3,
  minWidth: 0,
};

export const waitingReasonMetaStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  color: "var(--state-warning)",
};

export const renameInputStyle: React.CSSProperties = {
  width: "100%",
  background: "var(--surface-page)",
  border: "1px solid var(--accent-primary)",
  borderRadius: 4,
  padding: "2px 6px",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  outline: "none",
};
