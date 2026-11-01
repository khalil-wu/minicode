import { appendWorkspaceOwner, workspaceRequestPath } from "../protocol/workspace-owner";
import type * as Monaco from "monaco-editor/editor/editor.api.js";
import { CancellationError, isCancellationError } from "monaco-editor/base/common/errors.js";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, fetchWithTimeout, errorMessageFromResponseText } from "../protocol/api";
import { workspacePathWithin } from "../lib/workspace-path";
import { pushToast } from "../overlays/ToastContainer";

type Position = { line: number; character: number };
type Range = { start: Position; end: Position };
type TextEdit = { range: Range; newText: string };
type Location = { uri: string; range: Range } | { targetUri: string; targetSelectionRange: Range };
type DocumentSymbol = { name: string; detail?: string; kind: number; range: Range; selectionRange: Range; children?: DocumentSymbol[] };
type SymbolInformation = { name: string; kind: number; containerName?: string; location: { uri: string; range: Range } };
type CompletionItem = { label: string; kind?: number; detail?: string; documentation?: string | { value: string }; insertText?: string; insertTextFormat?: number; textEdit?: TextEdit; additionalTextEdits?: TextEdit[]; filterText?: string; sortText?: string };
type Diagnostic = { range: Range; message: string; severity?: number; code?: string | number; source?: string };
const serverLanguages = ["python", "yaml", "c", "cpp"];
const isCpp = (language: string) => language === "c" || language === "cpp";

type OwnedModel = {
  model: Monaco.editor.ITextModel;
  documentId: string;
  language: string;
  closed: boolean;
  subscriptions: Monaco.IDisposable[];
};
type LanguageOwner = {
  windowOwnerId: string;
  windowInstanceId: string;
  windowBindingToken: string;
  clientId: string;
  revision: number;
  released: boolean;
  models: WeakMap<Monaco.editor.ITextModel, OwnedModel>;
  liveModels: Set<OwnedModel>;
  pending: Set<{ controller: AbortController; models: OwnedModel[] }>;
  closes: Set<string>;
  closeFlight?: Promise<void>;
  unload: () => void;
};
const languageOwners = new WeakMap<typeof Monaco, LanguageOwner>();

async function sendLifecycle(owner: LanguageOwner, action: "close" | "release", documentIds: string[] = []): Promise<void> {
  const response = await fetchWithTimeout(new URL("/api/workspace/language/lifecycle", apiBase()), {
    method: "POST", keepalive: true, headers: authHeaders({ "content-type": "application/json", "x-minicode-window-binding-token": owner.windowBindingToken }),
    body: JSON.stringify({ action, window_owner_id: owner.windowOwnerId, window_instance_id: owner.windowInstanceId,
      client_id: owner.clientId, document_ids: documentIds }),
  });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
}

async function flushClosedModels(owner: LanguageOwner): Promise<void> {
  if (owner.released || owner.closes.size === 0) return;
  if (owner.closeFlight) {
    await owner.closeFlight;
    return flushClosedModels(owner);
  }
  const flight = (async () => {
    while (owner.closes.size > 0 && !owner.released) {
      const closed = [...owner.closes];
      await sendLifecycle(owner, "close", closed);
      closed.forEach((id) => owner.closes.delete(id));
    }
  })();
  owner.closeFlight = flight;
  try { await flight; }
  finally { owner.closeFlight = undefined; }
  await flushClosedModels(owner);
}

async function releaseOwner(owner: LanguageOwner): Promise<void> {
  owner.released = true;
  owner.pending.forEach(({ controller }) => controller.abort(new CancellationError()));
  window.removeEventListener("pagehide", owner.unload);
  owner.liveModels.forEach((record) => record.subscriptions.forEach((subscription) => subscription.dispose()));
  owner.liveModels.clear();
  await sendLifecycle(owner, "release");
}

export async function releaseEditorLanguageClient(monaco: typeof Monaco): Promise<void> {
  const owner = languageOwners.get(monaco);
  if (owner) await releaseOwner(owner);
}

function languageOwner(monaco: typeof Monaco): LanguageOwner {
  let owner = languageOwners.get(monaco);
  if (!owner || owner.released) {
    const clientId = crypto.randomUUID();
    const created: LanguageOwner = {
      windowOwnerId: window.__MINICODE_RUNTIME__?.windowOwnerId ?? "primary",
      windowInstanceId: window.__MINICODE_RUNTIME__?.windowInstanceId ?? clientId, clientId,
      windowBindingToken: window.__MINICODE_RUNTIME__?.windowBindingToken ?? crypto.randomUUID(),
      revision: 0, released: false, models: new WeakMap(), liveModels: new Set(), pending: new Set(), closes: new Set(),
      unload: () => { void releaseOwner(created).catch((error) => pushToast(String(error), "error")); },
    };
    window.addEventListener("pagehide", created.unload);
    languageOwners.set(monaco, created);
    owner = created;
  }
  return owner;
}

function retireModel(owner: LanguageOwner, record: OwnedModel): void {
  record.closed = true;
  record.subscriptions.forEach((subscription) => subscription.dispose());
  owner.liveModels.delete(record);
  owner.pending.forEach(({ controller, models }) => {
    if (models.includes(record)) controller.abort(new CancellationError());
  });
  owner.closes.add(record.documentId);
  void flushClosedModels(owner).catch((error) => pushToast(String(error), "error"));
}

function ownedModel(owner: LanguageOwner, model: Monaco.editor.ITextModel): OwnedModel {
  if (model.isDisposed()) throw new CancellationError();
  let record = owner.models.get(model);
  if (record && record.language !== model.getLanguageId()) {
    retireModel(owner, record);
    owner.models.delete(model);
    record = undefined;
  }
  if (record?.closed) throw new CancellationError();
  if (!record) {
    const created: OwnedModel = { model, documentId: crypto.randomUUID(), language: model.getLanguageId(), closed: false, subscriptions: [] };
    created.subscriptions.push(model.onDidChangeContent(() => {
      owner.pending.forEach(({ controller, models }) => {
        if (models.includes(created)) controller.abort(new CancellationError());
      });
    }), model.onWillDispose(() => retireModel(owner, created)));
    owner.models.set(model, created);
    owner.liveModels.add(created);
    record = created;
  }
  return record;
}

export const fromLspRange = (range: Range): Monaco.IRange => ({
  startLineNumber: range.start.line + 1, startColumn: range.start.character + 1,
  endLineNumber: range.end.line + 1, endColumn: range.end.character + 1,
});

export async function requestEditorLanguage<T>(monaco: typeof Monaco, model: Monaco.editor.ITextModel, method: string, position?: Monaco.IPosition, extra = {}, signal?: AbortSignal): Promise<T> {
  const owner = languageOwner(monaco);
  const root = useAppStore.getState().workingDirectory;
  const documents = [model, ...monaco.editor.getModels().filter((candidate) => candidate !== model && (candidate.getLanguageId() === model.getLanguageId()
    || (isCpp(model.getLanguageId()) && isCpp(candidate.getLanguageId())))
    && candidate.uri.scheme === "file" && workspacePathWithin(candidate.uri.fsPath, root))];
  const records = documents.map((doc) => ownedModel(owner, doc));
  const snapshot = documents.map((doc, index) => ({ document_id: records[index].documentId, model_version: doc.getVersionId(),
    path: workspaceRequestPath(doc.uri.fsPath, root), content: doc.getValue(), language: doc.getLanguageId() }));
  const revision = ++owner.revision;
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason ?? new CancellationError());
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const pending = { controller, models: records };
  owner.pending.add(pending);
  try {
    // Disposal has its own message. It must reach didClose before another
    // snapshot can use the discarded document's old language-server state.
    await flushClosedModels(owner);
    if (controller.signal.aborted || owner.released || records.some((record) => record.closed)) throw new CancellationError();
    const url = new URL("/api/workspace/language", apiBase());
    url.searchParams.set("workspace_root", root);
  appendWorkspaceOwner(url);
    const response = await fetchWithTimeout(url, { method: "POST", signal: controller.signal,
      headers: authHeaders({ "content-type": "application/json", "x-minicode-window-binding-token": owner.windowBindingToken }),
      body: JSON.stringify({ method, window_owner_id: owner.windowOwnerId, window_instance_id: owner.windowInstanceId,
        client_id: owner.clientId, snapshot_revision: revision, documents: snapshot,
        line: (position?.lineNumber ?? 1) - 1, character: (position?.column ?? 1) - 1, ...extra }),
    });
    if (!response.ok) {
      const body = await response.text();
      if (response.status === 409 && JSON.parse(body).detail?.code === "editor.document_stale") throw new CancellationError();
      throw new Error(errorMessageFromResponseText(body, response.statusText));
    }
    const result = (await response.json()).result as T;
    if (controller.signal.aborted || records.some((record, index) => record.closed || record.model.getVersionId() !== snapshot[index].model_version)) throw new CancellationError();
    return result;
  } finally {
    owner.pending.delete(pending);
    signal?.removeEventListener("abort", abort);
  }
}

export function registerEditorLanguageServices(monaco: typeof Monaco): Monaco.IDisposable {
  const disposables: Monaco.IDisposable[] = [];
  const request = async <T>(model: Monaco.editor.ITextModel, method: string, token: Monaco.CancellationToken, position?: Monaco.IPosition, extra = {}) => {
    if (token.isCancellationRequested) throw new CancellationError();
    const controller = new AbortController();
    const subscription = token.onCancellationRequested(() => controller.abort(new CancellationError()));
    try { return await requestEditorLanguage<T>(monaco, model, method, position, extra, controller.signal); }
    finally { subscription.dispose(); }
  };
  const asLocations = (value: Location | Location[] | null): Monaco.languages.Location[] => (value == null ? [] : Array.isArray(value) ? value : [value])
    .map((location) => "targetUri" in location
      ? { uri: monaco.Uri.parse(location.targetUri), range: fromLspRange(location.targetSelectionRange) }
      : { uri: monaco.Uri.parse(location.uri), range: fromLspRange(location.range) });
  const symbol = (item: DocumentSymbol): Monaco.languages.DocumentSymbol => ({
    name: item.name, detail: item.detail ?? "", kind: item.kind - 1, tags: [],
    range: fromLspRange(item.range), selectionRange: fromLspRange(item.selectionRange), children: item.children?.map(symbol),
  });
  const documentSymbol = (item: DocumentSymbol | SymbolInformation): Monaco.languages.DocumentSymbol => "location" in item
    ? { name: item.name, detail: item.containerName ?? "", kind: item.kind - 1, tags: [],
        range: fromLspRange(item.location.range), selectionRange: fromLspRange(item.location.range) }
    : symbol(item);
  const markdown = (value: string | { value: string } | undefined): Monaco.IMarkdownString => ({ value: typeof value === "string" ? value : value?.value ?? "", isTrusted: false });
  const kinds = monaco.languages.CompletionItemKind;
  const kindMap = [kinds.Text, kinds.Method, kinds.Function, kinds.Constructor, kinds.Field, kinds.Variable, kinds.Class,
    kinds.Interface, kinds.Module, kinds.Property, kinds.Unit, kinds.Value, kinds.Enum, kinds.Keyword, kinds.Snippet,
    kinds.Color, kinds.File, kinds.Reference, kinds.Folder, kinds.EnumMember, kinds.Constant, kinds.Struct, kinds.Event, kinds.Operator, kinds.TypeParameter];
  for (const language of serverLanguages) {
    disposables.push(
      monaco.languages.registerCompletionItemProvider(language, {
        triggerCharacters: isCpp(language) ? [".", ":", " ", '"', "'", "#", "<", "/", ">"] : [".", ":", " ", '"', "'"],
        async provideCompletionItems(model, position, _context, token) {
          const result = await request<CompletionItem[] | { items: CompletionItem[]; isIncomplete?: boolean } | null>(model, "completion", token, position);
          const word = model.getWordUntilPosition(position);
          const range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: word.endColumn };
          return { incomplete: result != null && !Array.isArray(result) && result.isIncomplete,
            suggestions: (Array.isArray(result) ? result : result?.items ?? []).map((item) => ({
              label: item.label, kind: kindMap[(item.kind ?? 1) - 1], detail: item.detail, documentation: markdown(item.documentation),
              insertText: item.textEdit?.newText ?? item.insertText ?? item.label, range: item.textEdit ? fromLspRange(item.textEdit.range) : range,
              insertTextRules: item.insertTextFormat === 2 ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
              additionalTextEdits: item.additionalTextEdits?.map((edit) => ({ range: fromLspRange(edit.range), text: edit.newText })),
              filterText: item.filterText, sortText: item.sortText,
            })) };
        },
      }),
      monaco.languages.registerHoverProvider(language, {
        async provideHover(model, position, token) {
          const result = await request<{ contents: string | { value: string } | Array<string | { value: string }>; range?: Range } | null>(model, "hover", token, position);
          return result ? { contents: (Array.isArray(result.contents) ? result.contents : [result.contents]).map(markdown), range: result.range && fromLspRange(result.range) } : null;
        },
      }),
      monaco.languages.registerDocumentSymbolProvider(language, {
        displayName: language === "python" ? "Pyright" : language === "yaml" ? "YAML" : "clangd",
        async provideDocumentSymbols(model, token) { return (await request<Array<DocumentSymbol | SymbolInformation> | null>(model, "documentSymbol", token) ?? []).map(documentSymbol); },
      }),
      monaco.languages.registerDocumentFormattingEditProvider(language, {
        async provideDocumentFormattingEdits(model, options, token) {
          const result = await request<TextEdit[] | { text: string } | null>(model, "formatting", token, undefined, { tab_size: options.tabSize, insert_spaces: options.insertSpaces });
          return result == null ? [] : Array.isArray(result) ? result.map((edit) => ({ range: fromLspRange(edit.range), text: edit.newText }))
            : [{ range: model.getFullModelRange(), text: result.text }];
        },
      }),
    );
    if (language === "python" || isCpp(language)) disposables.push(
      monaco.languages.registerDefinitionProvider(language, { async provideDefinition(model, position, token) { return asLocations(await request<Location[] | Location | null>(model, "definition", token, position)); } }),
      monaco.languages.registerReferenceProvider(language, { async provideReferences(model, position, _context, token) { return asLocations(await request<Location[] | null>(model, "references", token, position)); } }),
      monaco.languages.registerRenameProvider(language, {
        async provideRenameEdits(model, position, newName, token) {
          const result = await request<{ changes?: Record<string, TextEdit[]>; documentChanges?: Array<{ textDocument: { uri: string }; edits: TextEdit[] }> }>(model, "rename", token, position, { new_name: newName });
          const changes = [...Object.entries(result.changes ?? {}), ...(result.documentChanges ?? []).map((doc) => [doc.textDocument.uri, doc.edits] as const)];
          // A rename may touch documents that are not open; load them into
          // tracked buffers so the bulk edit can apply and the user sees the result.
          const { ensureWorkspaceEditModels } = await import("./applyWorkspaceBufferEdits");
          await ensureWorkspaceEditModels(monaco, useAppStore.getState().workingDirectory, changes.map(([uri]) => monaco.Uri.parse(uri)));
          if (token.isCancellationRequested) return { edits: [] };
          return { edits: changes.flatMap(([uri, edits]) => edits.map((edit) => ({ resource: monaco.Uri.parse(uri), textEdit: { range: fromLspRange(edit.range), text: edit.newText }, versionId: undefined }))) };
        },
      }),
      monaco.languages.registerSignatureHelpProvider(language, {
        signatureHelpTriggerCharacters: ["(", ","],
        async provideSignatureHelp(model, position, token) {
          const result = await request<Monaco.languages.SignatureHelp | null>(model, "signatureHelp", token, position);
          return result ? { value: { ...result, activeSignature: result.activeSignature ?? 0, activeParameter: result.activeParameter ?? 0 }, dispose() {} } : null;
        },
      }),
    );
  }
  const modelSubscriptions = new Map<Monaco.editor.ITextModel, () => void>();
  const observe = (model: Monaco.editor.ITextModel) => {
    if (!serverLanguages.includes(model.getLanguageId()) || model.uri.scheme !== "file" || modelSubscriptions.has(model)) return;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | undefined;
    const scan = () => {
      clearTimeout(timer);
      controller?.abort();
      timer = setTimeout(async () => {
        const root = useAppStore.getState().workingDirectory;
        if (!root || !workspacePathWithin(model.uri.fsPath, root)) return;
        const current = controller = new AbortController();
        const version = model.getVersionId();
        try {
          const diagnostics = await requestEditorLanguage<Diagnostic[]>(monaco, model, "diagnostics", undefined, {}, current.signal);
          if (!model.isDisposed() && version === model.getVersionId() && !current.signal.aborted) monaco.editor.setModelMarkers(model, "minicode-language", diagnostics.map((item) => ({
            ...fromLspRange(item.range), message: item.message, code: item.code?.toString(), source: item.source ?? model.getLanguageId(),
            severity: [monaco.MarkerSeverity.Error, monaco.MarkerSeverity.Warning, monaco.MarkerSeverity.Info, monaco.MarkerSeverity.Hint][(item.severity ?? 1) - 1],
          })));
        } catch (error) {
          if (!current.signal.aborted && !model.isDisposed() && !isCancellationError(error)) monaco.editor.setModelMarkers(model, "minicode-language", [{
            startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 1, severity: monaco.MarkerSeverity.Warning,
            source: "语言服务", message: error instanceof Error ? error.message : String(error),
          }]);
        }
      }, 500);
    };
    const change = model.onDidChangeContent(scan);
    const dispose = () => { clearTimeout(timer); controller?.abort(); change.dispose(); modelSubscriptions.delete(model); };
    modelSubscriptions.set(model, dispose);
    model.onWillDispose(dispose);
    scan();
  };
  monaco.editor.getModels().forEach(observe);
  disposables.push(monaco.editor.onDidCreateModel(observe), monaco.editor.onDidChangeModelLanguage(({ model }) => {
    const owner = languageOwners.get(monaco);
    const record = owner?.models.get(model);
    if (owner && record) { retireModel(owner, record); owner.models.delete(model); }
    modelSubscriptions.get(model)?.();
    monaco.editor.setModelMarkers(model, "minicode-language", []);
    observe(model);
  }));
  return { dispose() { disposables.forEach((item) => item.dispose()); modelSubscriptions.forEach((dispose) => dispose()); } };
}
