import { useEffect, useRef, useState } from "react";
import type { GitDiffContentResult } from "../../protocol/workspace-types";
import type { GitDiffSnapshot } from "../../stores/types";
import { readGitFileContents } from "./git-file-contents";

export function useGitDiffContents({ path, open, snapshot, enabled, format, sides = "both" }: {
  path: string; open: boolean; snapshot?: GitDiffSnapshot; enabled: boolean;
  format: "text" | "binary" | "markdown-preview"; sides?: "both" | "new";
}) {
  const element = useRef<HTMLDivElement>(null);
  const [visibleKey, setVisibleKey] = useState<string | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
  const key = JSON.stringify([snapshot, path, format, sides, retryNonce]);
  const request = useRef<{ key: string; promise: Promise<GitDiffContentResult[]> }>();
  const [state, setState] = useState<{ key: string; results?: GitDiffContentResult[]; error?: Error }>();

  useEffect(() => {
    setVisibleKey(null);
    if (!open || !enabled) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        setVisibleKey(key);
        observer.disconnect();
      }
    });
    observer.observe(element.current!);
    return () => observer.disconnect();
  }, [open, enabled, key]);

  useEffect(() => {
    if (!open || !enabled || !snapshot || visibleKey !== key) return;
    if (request.current?.key !== key) request.current = { key, promise: readGitFileContents(snapshot,
      (sides === "both" ? ["old", "new"] as const : ["new"] as const).map(side => ({ path, side, format }))) };
    let mounted = true;
    request.current.promise.then(results => {
      if (mounted) setState({ key, results });
    }, error => {
      if (mounted) setState({ key, error });
    });
    return () => { mounted = false; };
  }, [open, enabled, key, visibleKey]);

  return { element, results: state?.key === key ? state.results : undefined,
    error: state?.key === key ? state.error : undefined,
    retry: () => setRetryNonce(value => value + 1) };
}
