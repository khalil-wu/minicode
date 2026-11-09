import { HTMLWorker } from "monaco-editor/languages/features/html/htmlWorker.js";

/** Supply the document context omitted by Monaco 0.56's HTML worker. */
export class WorkspaceHTMLWorker extends HTMLWorker {
  constructor(context: unknown, createData: unknown) {
    super(context, createData);
  }

  async findDocumentLinks(uri: string) {
    const document = this._getTextDocument(uri);
    if (!document) return [];
    // Resolve before the link adapter converts a relative URL to a file URI.
    // The native scanner also uses this context to resolve <base href>.
    return this._languageService.findDocumentLinks(document, {
      resolveReference: (reference: string, base: string) => new URL(reference, base).href,
    });
  }
}
