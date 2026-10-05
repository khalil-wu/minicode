import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText } from "../protocol/api";
import { workspacePathWithin } from "../lib/workspace-path";

export async function generateEditorCode(workspaceRoot: string, data: { path: string; prefix: string; suffix: string; selected?: string; instruction?: string; mode?: "complete" | "edit" }, signal: AbortSignal) {
  const state = useAppStore.getState();
  const preferences = state.workbenchPreferences;
  const url = new URL("/api/workspace/inline-completion", apiBase());
  url.searchParams.set("workspace_root", workspaceRoot);
  const response = await fetch(url, { method: "POST", signal, headers: authHeaders({ "content-type": "application/json" }),
    body: JSON.stringify({ ...data, prefix: data.prefix.slice(-16000), suffix: data.suffix.slice(0, 8000), max_tokens: data.mode === "edit" ? 4096 : preferences.aiMaxTokens,
      model: preferences.aiModel || state.currentModel, provider: preferences.aiProvider || state.currentProviderId || state.currentProvider }) });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  const result = await response.json() as { text: string; error?: string; usage: { input_tokens: number; output_tokens: number }; model: string };
  if (result.error) throw new Error(result.error);
  useAppStore.setState((current) => ({ inlineCompletionUsage: { requests: current.inlineCompletionUsage.requests + 1,
    inputTokens: current.inlineCompletionUsage.inputTokens + result.usage.input_tokens, outputTokens: current.inlineCompletionUsage.outputTokens + result.usage.output_tokens, lastError: "" } }));
  return result;
}

export function registerInlinePrediction(monaco: typeof Monaco) {
  return monaco.languages.registerInlineCompletionsProvider({ pattern: "**" }, {
    displayName: "MiniCode", debounceDelayMs: 650,
    async provideInlineCompletions(model, position, _context, token) {
      const state = useAppStore.getState();
      if (!state.workbenchPreferences.aiEnabled || model.uri.scheme !== "file" || !workspacePathWithin(model.uri.fsPath, state.workingDirectory)) return { items: [] };
      const version = model.getVersionId();
      const controller = new AbortController();
      const cancellation = token.onCancellationRequested(() => controller.abort());
      const unsubscribe = useAppStore.subscribe((next) => {
        if (next.workingDirectory !== state.workingDirectory || !next.workbenchPreferences.aiEnabled) controller.abort();
      });
      try {
        const content = model.getValue();
        const offset = model.getOffsetAt(position);
        const result = await generateEditorCode(state.workingDirectory, { path: model.uri.fsPath, prefix: content.slice(0, offset), suffix: content.slice(offset) }, controller.signal);
        if (token.isCancellationRequested || model.isDisposed() || model.getVersionId() !== version) return { items: [] };
        return { items: result.text ? [{ insertText: result.text, range: new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column) }] : [] };
      } catch (error) {
        if (!controller.signal.aborted) useAppStore.setState((current) => ({ inlineCompletionUsage: { ...current.inlineCompletionUsage, lastError: error instanceof Error ? error.message : String(error) } }));
        return { items: [] };
      } finally { cancellation.dispose(); unsubscribe(); }
    },
    disposeInlineCompletions() {},
  });
}
