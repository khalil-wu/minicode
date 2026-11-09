import { initialize } from "monaco-editor/internal/common/initialize.js";
import { WorkspaceHTMLWorker } from "./workspaceHtmlService";

self.onmessage = () => {
  initialize((context, createData) => new WorkspaceHTMLWorker(context, createData));
};
