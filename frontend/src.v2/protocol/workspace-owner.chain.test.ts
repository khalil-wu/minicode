// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
const scope = vi.hoisted(() => ({ sessionId: "session-a", conversationId: "conversation-a", root: "C:/view", prompt: vi.fn() }));
vi.mock("../stores", () => ({ useAppStore: { getState: () => ({ conversationId: scope.conversationId, workingDirectory: scope.root }) } }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: scope.sessionId }) }));
vi.mock("../overlays/DialogService", () => ({ showPrompt: scope.prompt }));
import { readWorkspaceFile, compareWriteWorkspaceFile } from "./workspace";
import { bindWorkspaceOwnerRuntime, workspaceFileOwner } from "./workspace-owner";
import { fsListTree, pickDirectory, pickWorkspaceDirectory } from "../desktop/runtime";
import { artifactResourceUrl } from "../lib/artifact-resource";

beforeEach(() => { scope.sessionId = "session-a"; scope.conversationId = "conversation-a"; scope.prompt.mockReset(); vi.unstubAllGlobals();
  bindWorkspaceOwnerRuntime(() => ({ session_id: scope.sessionId, conversation_id: scope.conversationId, workspace_root: scope.root })); });

describe("actual workspace request ownership", () => {
  it("captures the read target for a later save while another conversation is active", async () => {
    const file = { content: "source bytes", content_hash: "hash", path_uri: "file:///remote/project/same.txt", environment_id: "target",
      session_id: "session-a", conversation_id: "conversation-a" };
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(file)))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Workspace conversation owner is stale." }), { status: 409 }));
    vi.stubGlobal("fetch", fetch);
    const read = await readWorkspaceFile("C:/view/same.txt", scope.root);
    expect(Object.fromEntries(new URL(fetch.mock.calls[0][0]).searchParams)).toMatchObject({ path: "same.txt", session_id: "session-a", conversation_id: "conversation-a" });
    scope.conversationId = "conversation-b";
    scope.sessionId = "reconnected-session";
    const result = await compareWriteWorkspaceFile("same.txt", "hash", "unsaved draft", scope.root, workspaceFileOwner(read));
    expect(result).toMatchObject({ ok: false, conflict: false, message: "Workspace conversation owner is stale." });
    const url = new URL(fetch.mock.calls[1][0]);
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ session_id: "reconnected-session", conversation_id: "conversation-a", environment_id: "target" });
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toMatchObject({ path: file.path_uri, content: "unsaved draft" });
  });

  it("routes a connected desktop tree through HTTP without falling back to native FS", async () => {
    const native = vi.fn();
    window.__MINICODE_RUNTIME__ = { desktop: { fs: { listTree: native } } as never };
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ workspace_root: "file:///remote/project", requested_path: ".",
      entries: [{ name: "source.py", path: "source.py", is_dir: false }] })));
    vi.stubGlobal("fetch", fetch);
    expect(await fsListTree("C:/view")).toMatchObject([{ path: "C:/view/source.py", name: "source.py" }]);
    expect(native).not.toHaveBeenCalled();
    expect(new URL(fetch.mock.calls[0][0]).searchParams.get("conversation_id")).toBe("conversation-a");
    fetch.mockRejectedValueOnce(new Error("actual executor disconnected"));
    await expect(fsListTree("C:/view")).rejects.toThrow("actual executor disconnected");
    expect(native).not.toHaveBeenCalled();
  });

  it("refreshes a preview token without adopting the newly selected conversation", () => {
    scope.conversationId = "conversation-b";
    const url = new URL(artifactResourceUrl({ source: "workspace", isConnected: true, sessionId: "session-new",
      originalUrl: "http://old/api/workspace/raw?path=file%3A%2F%2F%2Fremote%2Fa.png&workspace_root=C%3A%2Fview&session_id=session-a&conversation_id=conversation-a&environment_id=target" }));
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ session_id: "session-new", conversation_id: "conversation-a", environment_id: "target" });
  });

  it("uses the selected remote host path prompt and trust operation", async () => {
    const nativePicker = vi.fn(), trust = vi.fn(async path => path);
    window.__MINICODE_RUNTIME__ = { remoteHost: { id: "remote", displayName: "Remote", kind: "ssh", platform: "linux", home: "/home/owned", generation: "generation" },
      desktop: { pickDirectory: nativePicker, pickWorkspaceDirectory: nativePicker, trustWorkspace: trust } as never };
    scope.prompt.mockResolvedValueOnce("/home/owned/selected").mockResolvedValueOnce("/home/owned/project");
    expect(await pickDirectory()).toBe("/home/owned/selected");
    expect(await pickWorkspaceDirectory()).toBe("/home/owned/project");
    expect(scope.prompt).toHaveBeenCalledWith(expect.objectContaining({ defaultValue: "/home/owned" }));
    expect(trust).toHaveBeenCalledWith("/home/owned/project");
    expect(nativePicker).not.toHaveBeenCalled();
  });
});
