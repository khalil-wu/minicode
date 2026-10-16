export { workspaceRequestPath } from "./workspace-request-path";

export interface WorkspaceRequestOwner {
  session_id: string;
  conversation_id: string;
  environment_id?: string;
  path_uri?: string;
}

type WorkspaceRuntimeOwner = { session_id: string; conversation_id: string; workspace_root: string };
let readRuntimeOwner: () => WorkspaceRuntimeOwner | undefined = () => undefined;
export function bindWorkspaceOwnerRuntime(read: () => WorkspaceRuntimeOwner | undefined): void { readRuntimeOwner = read; }

export function workspaceRequestOwner(): WorkspaceRequestOwner | undefined {
  const owner = readRuntimeOwner();
  return owner?.session_id && owner.conversation_id ? { session_id: owner.session_id, conversation_id: owner.conversation_id } : undefined;
}

export function workspaceViewRoot(): string { return readRuntimeOwner()?.workspace_root ?? ""; }
export function workspaceHasConversation(): boolean { return Boolean(readRuntimeOwner()?.conversation_id); }

export function appendWorkspaceOwner(url: URL, owner = workspaceRequestOwner()): void {
  if (!owner) return;
  url.searchParams.set("session_id", readRuntimeOwner()?.session_id || owner.session_id);
  url.searchParams.set("conversation_id", owner.conversation_id);
  if (owner.environment_id) url.searchParams.set("environment_id", owner.environment_id);
}

export function workspaceFileOwner(file: { session_id?: string; conversation_id?: string; environment_id?: string; path_uri?: string }): WorkspaceRequestOwner | undefined {
  return file.session_id && file.conversation_id ? { session_id: file.session_id, conversation_id: file.conversation_id,
    environment_id: file.environment_id, path_uri: file.path_uri } : undefined;
}
