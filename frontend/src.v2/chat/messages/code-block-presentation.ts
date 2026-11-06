import type { CSSProperties } from "react";

export const codeBlockStyle = (hasLanguage: boolean): CSSProperties => ({
  margin: 0, padding: "12px 14px", background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: hasLanguage ? "0 0 var(--radius-sm, 6px) var(--radius-sm, 6px)" : "var(--radius-sm, 6px)",
  fontFamily: "var(--editor-font-family)", fontSize: "var(--editor-font-size)",
  lineHeight: "var(--editor-line-height)", color: "var(--editor-foreground)",
  whiteSpace: "pre-wrap", overflowWrap: "anywhere",
});

export const lineNumberColumnStyle: CSSProperties = {
  display: "inline-block", minWidth: "2.25em", paddingRight: "1.25em",
  color: "var(--editor-line-number)", opacity: .8, userSelect: "none",
};
