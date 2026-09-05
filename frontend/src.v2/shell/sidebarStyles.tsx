import React from "react";

export const modeSwitchStyle: React.CSSProperties = {
  position: "relative",
  zIndex: 1,
  isolation: "isolate",
  display: "grid",
  // minmax(0,1fr): plain 1fr tracks floor at the button's min-content width,
  // so scaled CJK labels could push the second pill out of the container.
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
  gap: 2,
  height: 40,
  minHeight: 40,
  width: "100%",
  boxSizing: "border-box",
  padding: 2,
  overflow: "hidden",
  background: "var(--surface-page)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 10px)",
};

export const modeSwitchButtonStyle: React.CSSProperties = {
  height: 34,
  minHeight: 34,
  boxSizing: "border-box",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 6,
  minWidth: 0,
  padding: "0 8px",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm, 8px)",
  cursor: "pointer",
  fontSize: "var(--mc-font-body, var(--text-sm))",
  letterSpacing: 0,
};

export const modeSwitchLabelStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
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

export const projectCountStyle: React.CSSProperties = {
  fontFamily: "var(--font-ui)",
  fontWeight: "var(--fw-medium)",
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
  minHeight: 34,
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
  fontSize: "var(--text-chrome)",
  fontFamily: "var(--font-ui)",
  fontWeight: "var(--fw-medium)",
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
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontSize: "var(--text-2xs)",
  color: "var(--text-muted)",
  marginTop: 2,
};

export const renameInputStyle: React.CSSProperties = {
  width: "100%",
  background: "var(--surface-base)",
  border: "1px solid var(--accent-primary)",
  borderRadius: 4,
  padding: "2px 6px",
  color: "var(--text-primary)",
  fontFamily: "var(--font-ui)",
  fontSize: "var(--text-chrome)",
  outline: "none",
};
