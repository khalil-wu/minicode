import { registerToolRenderer } from "../toolRendererRegistry";
import { CommandToolRenderer } from "./CommandRenderer";
import { FileChangeToolRenderer } from "./FileChangeRenderer";
import { WebSearchToolRenderer } from "./WebSearchRenderer";

export function registerBuiltInToolRenderers(): void {
  for (const name of ["run_command", "shell_command", "bash", "powershell", "terminal"]) {
    registerToolRenderer(name, CommandToolRenderer);
  }
  for (const name of ["web_search", "search_web"]) {
    registerToolRenderer(name, WebSearchToolRenderer);
  }
  for (const name of ["write_file", "edit_file", "apply_patch", "delete_file", "create_file", "file_write", "file_edit"]) {
    registerToolRenderer(name, FileChangeToolRenderer);
  }
}

export { CommandResultView } from "./CommandRenderer";
export { FileChangeToolRenderer } from "./FileChangeRenderer";
export { WebSearchResultsView } from "./WebSearchRenderer";
