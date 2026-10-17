import { useEffect, useState } from "react";
import type { Parser } from "@lezer/common";
import { diffPresentationLanguage, type DiffPresentationLanguage } from "./file-diff-presentation";

const parsers = new Map<DiffPresentationLanguage, Promise<Parser>>();

export function loadDiffPresentationParser(language: DiffPresentationLanguage): Promise<Parser> {
  let parser = parsers.get(language);
  if (!parser) {
    switch (language) {
      case "javascript": case "typescript": case "tsx":
        parser = import("@lezer/javascript").then(({ parser }) => parser.configure({
          dialect: language === "javascript" ? "jsx" : language === "typescript" ? "ts" : "ts jsx",
        }));
        break;
      case "python": parser = import("@lezer/python").then(module => module.parser); break;
      case "rust": parser = import("@lezer/rust").then(module => module.parser); break;
      case "go": parser = import("@lezer/go").then(module => module.parser); break;
      case "java": parser = import("@lezer/java").then(module => module.parser); break;
    }
    parsers.set(language, parser);
  }
  return parser;
}

export function useDiffPresentationParser(path: string, enabled: boolean): Parser | undefined {
  const language = enabled ? diffPresentationLanguage(path) : undefined;
  const [state, setState] = useState<{ language: DiffPresentationLanguage; parser?: Parser; error?: Error }>();
  useEffect(() => {
    if (!language) return;
    let current = true;
    loadDiffPresentationParser(language).then(parser => {
      if (current) setState({ language, parser });
    }, error => {
      if (current) setState({ language, error });
    });
    return () => { current = false; };
  }, [language]);
  if (state?.language === language && state?.error) throw state.error;
  return state?.language === language ? state?.parser : undefined;
}
