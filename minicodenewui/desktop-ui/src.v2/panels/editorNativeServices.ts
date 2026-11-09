import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import { ILanguageFeaturesService } from "monaco-editor/editor/common/services/languageFeatures.js";

interface ProviderRegistry<T> { ordered(model: Monaco.editor.ITextModel): T[] }
interface LanguageFeatures {
  documentSymbolProvider: ProviderRegistry<Monaco.languages.DocumentSymbolProvider>;
  documentFormattingEditProvider: ProviderRegistry<Monaco.languages.DocumentFormattingEditProvider>;
}
export async function documentSymbols(model: Monaco.editor.ITextModel, token: Monaco.CancellationToken) {
  const providers = StandaloneServices.get<LanguageFeatures>(ILanguageFeaturesService).documentSymbolProvider.ordered(model);
  const results = await Promise.all(providers.map((provider) => provider.provideDocumentSymbols(model, token)));
  return results.flatMap((result) => result ?? []);
}
export async function formatEditorModel(monaco: typeof Monaco, model: Monaco.editor.ITextModel): Promise<void> {
  const provider = StandaloneServices.get<LanguageFeatures>(ILanguageFeaturesService).documentFormattingEditProvider.ordered(model)[0];
  if (!provider) return;
  const source = new monaco.CancellationTokenSource();
  const version = model.getVersionId();
  try {
    const edits = await provider.provideDocumentFormattingEdits(model, model.getOptions(), source.token);
    if (model.isDisposed() || model.getVersionId() !== version) throw new Error("格式化期间文件已修改，请重新保存。");
    if (edits?.length) {
      model.pushStackElement();
      model.pushEditOperations([], edits.map((edit) => ({ ...edit, forceMoveMarkers: true })), () => null);
      model.pushStackElement();
    }
  } finally { source.dispose(); }
}
