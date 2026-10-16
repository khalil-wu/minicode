import { useEffect, useState, type CSSProperties } from "react";
import { useAppStore } from "../../stores";
import { highlightCode } from "../../lib/code-highlighting";
import type { ThemedToken } from "@shikijs/core";
import type { ResolvedTheme } from "../../stores/types";

interface Props { children: string; hasLanguage: boolean; language: string; resolvedTheme: ResolvedTheme }

export default function CodeThemeHighlighter({ children, hasLanguage, language, resolvedTheme }: Props) {
  const id = useAppStore(state => state.appearanceThemes[resolvedTheme]?.codeThemeId ?? "vscode-plus");
  const [result, setResult] = useState<{ code: string; language: string; id: string; variant: string; lines: ThemedToken[][] }>();
  useEffect(() => {
    let current = true;
    void highlightCode(children, language, id, resolvedTheme).then(lines => {
      if (current) setResult({ code: children, language, id, variant: resolvedTheme, lines });
    });
    return () => { current = false; };
  }, [children, language, id, resolvedTheme]);
  const lines = result?.code === children && result.language === language && result.id === id && result.variant === resolvedTheme
    ? result.lines : children.split("\n").map(content => [{ content }]);
  return <pre className="overflow-x-auto" style={{
    margin: 0, padding: "12px 14px", background: "var(--surface-soft)", border: "1px solid var(--border-subtle)",
    borderRadius: hasLanguage ? "0 0 var(--radius-sm, 6px) var(--radius-sm, 6px)" : "var(--radius-sm, 6px)",
    fontFamily: "var(--editor-font-family)", fontSize: "var(--editor-font-size)", lineHeight: "var(--editor-line-height)", color: "var(--editor-foreground)",
    whiteSpace: "pre-wrap", overflowWrap: "anywhere",
  }}><code>{lines.map((line, index) => <span className="block" key={index}>
    <span aria-hidden="true" className="code-theme-line-number" style={{ display: "inline-block", minWidth: "2.25em", color: "var(--editor-line-number)", opacity: .8, paddingRight: "1.25em", userSelect: "none" }}>{index + 1}</span>
    {line.map((token, tokenIndex) => <span key={tokenIndex} style={{ color: "color" in token ? token.color : undefined } as CSSProperties}>{token.content}</span>)}
  </span>)}</code></pre>;
}
