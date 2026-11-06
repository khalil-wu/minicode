import { useEffect, useState, type CSSProperties } from "react";
import { useAppStore } from "../../stores";
import { highlightCodeInWorker } from "../../lib/code-highlighting-worker-client";
import type { ThemedToken } from "@shikijs/core";
import type { ResolvedTheme } from "../../stores/types";
import { codeBlockStyle, lineNumberColumnStyle } from "./code-block-presentation";

interface Props { children: string; hasLanguage: boolean; language: string; resolvedTheme: ResolvedTheme }

export default function CodeThemeHighlighter({ children, hasLanguage, language, resolvedTheme }: Props) {
  const id = useAppStore(state => state.appearanceThemes[resolvedTheme]?.codeThemeId ?? "codex");
  const [result, setResult] = useState<{ code: string; language: string; id: string; variant: string; lines: ThemedToken[][] }>();
  useEffect(() => {
    let current = true;
    void highlightCodeInWorker(children, language, id, resolvedTheme).then(lines => {
      if (current) setResult({ code: children, language, id, variant: resolvedTheme, lines });
    }).catch(error => {
      if (current) console.error("Code highlighting failed", error);
    });
    return () => { current = false; };
  }, [children, language, id, resolvedTheme]);
  const lines = result?.code === children && result.language === language && result.id === id && result.variant === resolvedTheme
    ? result.lines : children.split("\n").map(content => [{ content }]);
  return <pre className="overflow-x-auto" style={codeBlockStyle(hasLanguage)}><code>{lines.map((line, index) => <span className="block" key={index}>
    <span aria-hidden="true" className="code-theme-line-number" style={lineNumberColumnStyle}>{index + 1}</span>
    {line.map((token, tokenIndex) => <span key={tokenIndex} style={{ color: "color" in token ? token.color : undefined } as CSSProperties}>{token.content}</span>)}
  </span>)}</code></pre>;
}
