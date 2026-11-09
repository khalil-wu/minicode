import { highlightCode } from "./code-highlighting";
import type { CodeThemeId, CodeThemeVariant } from "./code-themes";

type Request = { requestId: number; code: string; language: string; theme: CodeThemeId; variant: CodeThemeVariant };

self.onmessage = async ({ data }: MessageEvent<Request>) => {
  const { requestId, code, language, theme, variant } = data;
  try {
    self.postMessage({ requestId, lines: await highlightCode(code, language, theme, variant) });
  } catch (error) {
    self.postMessage({ requestId, error: error instanceof Error ? error.message : String(error) });
  }
};
