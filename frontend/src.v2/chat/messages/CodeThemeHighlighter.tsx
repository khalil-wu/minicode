import { useEffect, useMemo, useState } from "react";
import { useAppStore } from "../../stores";
import { highlightCodeInWorker } from "../../lib/code-highlighting-worker-client";
import type { ResolvedTheme } from "../../stores/types";
import { codeBlockStyle } from "./code-block-presentation";

interface Props { children: string; hasLanguage: boolean; language: string; resolvedTheme: ResolvedTheme }

export default function CodeThemeHighlighter({ children, hasLanguage, language, resolvedTheme }: Props) {
  const id = useAppStore(state => state.appearanceThemes[resolvedTheme]?.codeThemeId ?? "codex");
  const [result, setResult] = useState<{ code: string; language: string; id: string; variant: string; lines: string[] }>();
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
    ? result.lines : undefined;
  const html = useMemo(() => lines?.map((line, index) => `<span class="block"><span aria-hidden="true" class="code-theme-line-number">${index + 1}</span>${line}</span>`).join(""), [lines]);
  return <pre className="overflow-x-auto" style={codeBlockStyle(hasLanguage)}>{html !== undefined
    ? <code dangerouslySetInnerHTML={{ __html: html }} />
    : <code>{children.split("\n").map((line, index) => <span className="block" key={index}>
      <span aria-hidden="true" className="code-theme-line-number">{index + 1}</span><span>{line}</span>
    </span>)}</code>}</pre>;
}
