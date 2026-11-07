import type { CSSProperties } from "react";

export const codeBlockStyle: CSSProperties = {
  margin: 0, padding: "12px 14px", background: "var(--surface-soft)",
  border: 0, borderRadius: 0,
  fontFamily: "var(--editor-font-family)", fontSize: "var(--editor-font-size)",
  lineHeight: "var(--editor-line-height)", color: "var(--editor-foreground)",
  whiteSpace: "pre-wrap", overflowWrap: "anywhere",
};
