export const WORKSPACE_TYPESCRIPT_METADATA_URI = "minicode://workspace/index.json";

export interface WorkspaceTypeScriptMetadata {
  workspaceRoot: string;
  sourceFileNames: string[];
  readOnlyFileNames: string[];
  caseSensitive: boolean;
}
