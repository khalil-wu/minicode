declare module "monaco-editor/editor/browser/services/bulkEditService.js" {
  import type * as Monaco from "monaco-editor/editor/editor.api.js";
  export const IBulkEditService: unknown;
  export class ResourceEdit {}
  export class ResourceTextEdit extends ResourceEdit {
    constructor(resource: Monaco.Uri, textEdit: Monaco.languages.TextEdit, versionId?: number, metadata?: unknown);
    readonly resource: Monaco.Uri;
    readonly textEdit: Monaco.languages.TextEdit;
    readonly versionId?: number;
    static is(edit: unknown): edit is ResourceTextEdit | Monaco.languages.IWorkspaceTextEdit;
  }
  export class ResourceFileEdit extends ResourceEdit {
    constructor(oldResource?: Monaco.Uri, newResource?: Monaco.Uri, options?: unknown, metadata?: unknown);
  }
  export interface BulkEditService {
    apply(edits: ResourceEdit[] | Monaco.languages.WorkspaceEdit, options?: unknown): Promise<{ ariaSummary: string; isApplied: boolean }>;
  }
}

declare module "monaco-editor/editor/standalone/browser/standaloneServices.js" {
  export namespace StandaloneServices {
    function get<T>(serviceId: unknown): T;
  }
}

declare module "monaco-editor/platform/undoRedo/common/undoRedo.js" {
  export class UndoRedoGroup {
    readonly id: number;
    nextOrder(): number;
  }
}
