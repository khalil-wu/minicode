import { initialize } from "monaco-editor/internal/common/initialize.js";
import { createWorkspaceTypeScriptService, type WorkspaceTypeScriptContext, type WorkspaceTypeScriptCreateData } from "./workspaceTypeScriptService";

self.onmessage = () => {
  initialize((context, createData) => createWorkspaceTypeScriptService(
    context as WorkspaceTypeScriptContext,
    createData as WorkspaceTypeScriptCreateData,
  ));
};
