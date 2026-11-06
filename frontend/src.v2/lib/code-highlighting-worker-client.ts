import type { ThemedToken } from "@shikijs/core";
import type { CodeThemeId, CodeThemeVariant } from "./code-themes";

let worker: Worker | undefined;
let failure: Error | undefined;
let sequence = 0;
const pending = new Map<number, { resolve: (lines: ThemedToken[][]) => void; reject: (error: Error) => void }>();

export function highlightCodeInWorker(code: string, language: string, theme: CodeThemeId, variant: CodeThemeVariant): Promise<ThemedToken[][]> {
  if (failure) return Promise.reject(failure);
  if (!worker) {
    worker = new Worker(new URL("./code-highlighting.worker.ts", import.meta.url), { type: "module" });
    worker.onmessage = ({ data }: MessageEvent<{ requestId: number; lines?: ThemedToken[][]; error?: string }>) => {
      const request = pending.get(data.requestId)!;
      pending.delete(data.requestId);
      if (data.error !== undefined) request.reject(new Error(data.error));
      else request.resolve(data.lines!);
    };
    worker.onerror = event => {
      failure = new Error(event.message);
      for (const request of pending.values()) request.reject(failure);
      pending.clear();
    };
  }
  const requestId = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    worker!.postMessage({ requestId, code, language, theme, variant });
  });
}

if (import.meta.hot) import.meta.hot.dispose(() => worker?.terminate());
