import { useEffect, useState } from "react";
import { useAppStore } from "../../stores";
import { highlightCodeInWorker } from "../../lib/code-highlighting-worker-client";
import type { ResolvedTheme } from "../../stores/types";
import { codeBlockStyle } from "./code-block-presentation";
import { CodeBlockContent } from "./CodeBlockContent";

interface Props { children: string; hasLanguage: boolean; language: string; resolvedTheme: ResolvedTheme; isStreaming?: boolean }

export default function CodeThemeHighlighter({ children, hasLanguage, language, resolvedTheme, isStreaming = false }: Props) {
  const id = useAppStore(state => state.appearanceThemes[resolvedTheme]?.codeThemeId ?? "codex");
  const [result, setResult] = useState<{ code: string; language: string; variant: string; lines: string[] }>();
  useEffect(() => {
    if (isStreaming) return;
    let current = true;
    void highlightCodeInWorker(children, language, id, resolvedTheme).then(lines => {
      if (current) setResult({ code: children, language, variant: resolvedTheme, lines });
    }).catch(error => {
      if (current) console.error("Code highlighting failed", error);
    });
    return () => { current = false; };
  }, [children, language, id, resolvedTheme, isStreaming]);
  // A new palette replaces matching code once its worker result is ready.
  // Clearing it first rebuilt every line twice and flashed plain text.
  const lines = !isStreaming && result?.code === children && result.language === language && result.variant === resolvedTheme
    ? result.lines : undefined;
  return <pre className="overflow-x-auto" style={codeBlockStyle(hasLanguage)} data-markdown-copy="code-block" data-markdown-copy-text={children}>
    <code><CodeBlockContent text={children} highlightedLines={lines} /></code>
  </pre>;
}
