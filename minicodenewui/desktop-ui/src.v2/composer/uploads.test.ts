/* @vitest-environment jsdom */

import { waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  sessionId: "session-1" as string | null,
  uploadAttachment: vi.fn(),
  importRemoteFile: vi.fn(),
  pushToast: vi.fn(),
  command: vi.fn(), send: vi.fn(), createdOwner: "conv-upload-1", ownerRoots: new Map<string, string>(), listeners: new Set<(data: unknown) => void>(),
}));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(() => false),
    })),
  });
});

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => mocks.sessionId ? { sessionId: mocks.sessionId,
    subscribe: (listener: (data: unknown) => void) => { mocks.listeners.add(listener); return () => mocks.listeners.delete(listener); },
  } : null,
}));

vi.mock("../protocol/api", () => ({
  uploadAttachment: mocks.uploadAttachment,
  fetchDraftModelSelection: vi.fn(async (root: string) => ({ provider: "custom", current_model: "fixture", model: "fixture",
    available_models: ["fixture"], models_source: "configured", config_version: "fixture", config_cwd: root, effective_reasoning_effort: "off", available_permission_modes: ["confirm", "auto", "bypass"],
    feature_flags: {}, composer_commands: [], default_model_selection: { provider: "custom", model: "fixture", reasoning_effort: "off", conversation_id: "", working_directory: root } })),
}));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.send, sendClientCommandAwaitResult: mocks.command }));
vi.mock("../stores/shared-helpers", async load => ({ ...await load<typeof import("../stores/shared-helpers")>(), newConversationId: () => mocks.createdOwner }));

vi.mock("../desktop/runtime", async (importOriginal) => ({
  ...await importOriginal<typeof import("../desktop/runtime")>(),
  desktop: () => ({ attachments: { importRemoteFile: mocks.importRemoteFile }, preferences: { get: async () => ({ defaultPermissionMode: "confirm", defaultTaskDirectory: "" }) } }),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: mocks.pushToast,
}));

import { useAppStore } from "../stores";
import { emptyComposerDraft, flushComposerDrafts, loadComposerDrafts } from "../stores/composer-drafts";
import { handleCommandResultEvent } from "../chat/commandResultEvents";
import { handleSessionEvent } from "../chat/sessionEvents";
import { retainFailedComposerInput, restoreRetainedComposerInput, cancelRetainedInputEdit } from "./retainedComposerInputs";
const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as never;
const switchProjection = (command: { type: string; conversation_id: string; client_command_id?: string }, hydrating: boolean) => {
  const event = { type: "conversation.switched", conversation_id: command.conversation_id, is_hydrating: hydrating, context_pending: hydrating,
    client_command_id: command.client_command_id, client_command_type: command.type,
    conversation: { id: command.conversation_id, title: "created", workspace_root: mocks.ownerRoots.get(command.conversation_id) || "", messages: [] } } as never;
  handleSessionEvent(event, buffers); for (const listener of [...mocks.listeners]) listener(event);
};
afterEach(() => { useAppStore.setState({ isConnected: false }); });
import { buildPastedTextFile } from "./pastedText";
import { cancelComposerUpload, retryComposerAttachment, uploadComposerFiles, uploadComposerRemoteFiles } from "./uploads";

describe("composer uploads", () => {
  it("cancels uploads added during a recalled failed edit without inserting their late response into the restored draft", async () => {
    useAppStore.setState({ conversationId: "cancel-retry", workingDirectory: "C:/cancel-retry", activeConversationDraftId: null,
      draft: "newer draft", messages: [], conversationMessages: {}, retainedComposerInputs: {},
      conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, attachments: [], queuedEdit: undefined, retainedInputEdit: undefined });
    retainFailedComposerInput({ owner: { kind: "conversation", id: "cancel-retry", workspaceRoot: "C:/cancel-retry" },
      message: { id: "failed-cancel", role: "user", content: "original", timestamp: 1, artifacts: [] },
      draft: { ...emptyComposerDraft(), draft: "original" }, comments: [] });
    restoreRetainedComposerInput("failed-cancel");
    let finish!: (value: unknown) => void;
    let signal!: AbortSignal;
    mocks.uploadAttachment.mockImplementation((_session, _owner, _file, options) => {
      signal = options.signal;
      return new Promise(resolve => { finish = resolve; });
    });
    uploadComposerFiles([new File(["temporary"], "temporary.txt")]);
    await waitFor(() => expect(mocks.uploadAttachment).toHaveBeenCalledOnce());
    cancelRetainedInputEdit();
    expect(signal.aborted).toBe(true);
    finish({ conversation_id: "cancel-retry", attachment: { artifact_id: "late-upload", file_name: "temporary.txt", kind: "document" } });
    await flushComposerDrafts();
    expect(useAppStore.getState()).toMatchObject({ draft: "newer draft", attachments: [] });
    expect(useAppStore.getState().conversationWorkbenchStates["cancel-retry"].attachments).toEqual([]);
    expect(useAppStore.getState().retainedComposerInputs["failed-cancel"].draft.draft).toBe("original");
  });

  it("finishes an upload in the newer draft backup while a retained failed input is being edited", async () => {
    useAppStore.setState({ conversationId: "upload-backup", workingDirectory: "C:/upload-backup", activeConversationDraftId: null,
      composerDraftsReady: true, draft: "newer upload draft", messages: [], retainedComposerInputs: {},
      conversations: [{ id: "upload-backup", title: "Owner", updatedAt: "2026-10-29", workspaceRoot: "C:/upload-backup" }] });
    let complete!: (value: unknown) => void;
    mocks.uploadAttachment.mockImplementation(() => new Promise(resolve => { complete = resolve; }));
    uploadComposerFiles([new File(["newer original bytes"], "newer.txt", { type: "text/plain" })]);
    await waitFor(() => expect(mocks.uploadAttachment).toHaveBeenCalledOnce());
    const attachment = useAppStore.getState().attachments[0];
    retainFailedComposerInput({ owner: { kind: "conversation", id: "upload-backup", workspaceRoot: "C:/upload-backup" },
      message: { id: "failed-local", role: "user", content: "failed original", timestamp: 1, artifacts: [] },
      draft: { ...emptyComposerDraft(), draft: "failed original" }, comments: [] });
    restoreRetainedComposerInput("failed-local");
    complete({ conversation_id: "upload-backup", artifact_id: "finished-upload", attachment: {
      id: attachment.id, artifact_id: "finished-upload", file_name: "newer.txt", kind: "document", media_type: "text/plain" } });
    await waitFor(() => expect(useAppStore.getState().retainedInputEdit?.backup.attachments[0].status).toBe("ready"));
    expect(useAppStore.getState().attachments).toEqual([]);
    expect(useAppStore.getState().conversationWorkbenchStates["upload-backup"].attachments).toEqual([]);
    cancelRetainedInputEdit();
    expect(useAppStore.getState().draft).toBe("newer upload draft");
    expect(useAppStore.getState().attachments[0]).toMatchObject({ id: attachment.id, status: "ready", artifactId: "finished-upload", localFile: attachment.localFile });
  });
  beforeEach(() => {
    mocks.sessionId = "session-1";
    mocks.uploadAttachment.mockReset();
    mocks.importRemoteFile.mockReset();
    mocks.pushToast.mockReset();
    mocks.createdOwner = "conv-upload-1"; mocks.ownerRoots.clear();
    mocks.listeners.clear(); mocks.command.mockReset(); mocks.send.mockReset();
    mocks.command.mockImplementation(async (command, expected) => {
      if (command.type === "conversation.switch") throw new Error("Switch success is a canonical notification, not command.result");
      if (command.type === "conversation.create") {
        mocks.ownerRoots.set(command.conversation_id, command.workspace_root || "");
        handleSessionEvent({ type: "conversation.list", active_conversation_id: useAppStore.getState().conversationId,
          conversations: [...useAppStore.getState().conversations.filter(item => item.id !== command.conversation_id).map(item => ({ id: item.id, title: item.title, workspace_root: item.workspaceRoot || "" })),
            { id: command.conversation_id, title: "created", workspace_root: command.workspace_root || "", execution_cwd: command.workspace_root || "" }] } as never, buffers);
      }
      return { type: "command.result", command: expected, level: "success", message: "", data: { conversation_id: command.conversation_id, execution_cwd: mocks.ownerRoots.get(command.conversation_id) || "" } };
    });
    mocks.send.mockImplementation(command => { if (command.type === "conversation.switch") { switchProjection(command, true); switchProjection(command, false); } return true; });
    useAppStore.setState({
      ...emptyComposerDraft(), workspaceComposerDrafts: {},
      retainedComposerInputs: {},
      conversationId: null,
      conversations: [],
      attachments: [],
      workingDirectory: "",
      conversationWorkbenchStates: {},
      sideChats: {},
      isConnected: true,
    });
  });

  it("imports a remote batch on its original host and adopts only its original workspace", async () => {
    mocks.createdOwner = "remote-owner";
    let finish!: (value: unknown) => void;
    const receipt = (path: string, type = "text/plain") => ({
      conversation_id: "remote-owner", file_name: path.split("/").at(-1), artifact_id: `artifact:${path}`, doc_id: `doc:${path}`,
      attachment: { artifact_id: `artifact:${path}`, file_name: path.split("/").at(-1), media_type: type, kind: type.startsWith("image/") ? "image" : "document", data: "original bytes" },
      source: { path, name: path.split("/").at(-1), mediaType: type, sizeBytes: 17 },
    });
    mocks.importRemoteFile.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce(receipt("/srv/中文资料/second.txt"));
    useAppStore.setState({ workingDirectory: "/srv/project", draft: "original remote draft" });
    uploadComposerRemoteFiles(["/srv/中文资料/image.png", "/srv/中文资料/second.txt"]);
    useAppStore.setState({ conversations: [{ id: "later-chat", title: "later", workspaceRoot: "/srv/later" }] });
    useAppStore.getState().snapshotWorkbenchState();
    useAppStore.getState().applyConversationSwitched({ conversationId: "later-chat" });
    useAppStore.setState({ conversationId: "later-chat", workingDirectory: "/srv/later", draft: "later draft", attachments: [] });
    await waitFor(() => expect(mocks.importRemoteFile).toHaveBeenCalledTimes(2));
    finish(receipt("/srv/中文资料/image.png", "image/png"));
    await waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates["remote-owner"]?.attachments?.every(item => item.status === "ready")).toBe(true));
    expect(mocks.importRemoteFile.mock.calls.map(([request]) => request)).toEqual([
      { path: "/srv/中文资料/image.png", sessionId: "session-1", conversationId: "remote-owner", workspaceRoot: "/srv/project" },
      { path: "/srv/中文资料/second.txt", sessionId: "session-1", conversationId: "remote-owner", workspaceRoot: "/srv/project" },
    ]);
    const stored = useAppStore.getState().conversationWorkbenchStates["remote-owner"]!.attachments!;
    expect(stored).toHaveLength(2);
    expect(stored[0]).toMatchObject({ remoteSourcePath: "/srv/中文资料/image.png", type: "image/png", size: 17, conversationId: "remote-owner", workspaceRoot: "/srv/project" });
    expect(stored[0].localFile).toBeUndefined();
    expect(stored[0].dataUrl).toBeUndefined();
    expect(stored[0].attachment).not.toHaveProperty("data");
    expect(mocks.uploadAttachment).not.toHaveBeenCalled();
    expect(useAppStore.getState()).toMatchObject({ conversationId: "later-chat", draft: "later draft", attachments: [] });
  });

  it("retries a failed remote source in its retained owner after a chat switch", async () => {
    mocks.importRemoteFile.mockRejectedValueOnce(new Error("remote file read denied"));
    useAppStore.setState({ conversationId: "remote-owner", workingDirectory: "/srv/project", draft: "owner draft" });
    uploadComposerRemoteFiles(["/outside/报告.txt"]);
    await waitFor(() => expect(useAppStore.getState().attachments[0].status).toBe("error"));
    const attachment = useAppStore.getState().attachments[0];
    useAppStore.getState().snapshotWorkbenchState();
    useAppStore.setState({ conversationId: "other", workingDirectory: "/srv/other", draft: "untouched", attachments: [] });
    mocks.importRemoteFile.mockResolvedValueOnce({ conversation_id: "remote-owner", file_name: "报告.txt", artifact_id: "remote-artifact", doc_id: "remote-doc",
      attachment: { artifact_id: "remote-artifact", kind: "document", media_type: "text/plain" },
      source: { path: "/outside/报告.txt", name: "报告.txt", mediaType: "text/plain", sizeBytes: 25 } });
    expect(retryComposerAttachment(attachment.id)).toBe(true);
    await waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates["remote-owner"]?.attachments?.[0].status).toBe("ready"));
    expect(mocks.importRemoteFile).toHaveBeenLastCalledWith({ path: "/outside/报告.txt", sessionId: "session-1", conversationId: "remote-owner", workspaceRoot: "/srv/project" });
    expect(useAppStore.getState()).toMatchObject({ conversationId: "other", draft: "untouched", attachments: [] });
  });

  it("does not restore a removed attachment when its actual remote import settles late", async () => {
    let finish!: (value: unknown) => void;
    mocks.importRemoteFile.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    useAppStore.setState({ conversationId: "remote-owner", workingDirectory: "/srv/project" });
    uploadComposerRemoteFiles(["/srv/file.txt"]);
    const attachment = useAppStore.getState().attachments[0];
    cancelComposerUpload(attachment.id);
    useAppStore.getState().removeAttachment(attachment.id);
    finish({ conversation_id: "remote-owner", artifact_id: "late", attachment: { kind: "document" } });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(useAppStore.getState().attachments).toEqual([]);
    expect(useAppStore.getState().conversationWorkbenchStates["remote-owner"]?.attachments ?? []).toEqual([]);
  });

  it("uploads a side attachment to its owner without adopting that owner into the main composer", async () => {
    useAppStore.setState({ conversationId: "main", draft: "main draft", sideChats: {
      side: { id: "side", messages: [], isStreaming: false, draft: "side draft", attachments: [], workspaceRoot: "/side-project" },
    } });
    mocks.uploadAttachment.mockResolvedValue({ conversation_id: "side", artifact_id: "side-artifact", attachment: { id: "side-artifact", file_name: "note.txt", kind: "document", media_type: "text/plain", artifact_id: "side-artifact" } });
    uploadComposerFiles([new File(["side body"], "note.txt", { type: "text/plain" })], "side");
    await waitFor(() => expect(useAppStore.getState().sideChats.side.attachments?.[0].status).toBe("ready"));
    expect(mocks.uploadAttachment).toHaveBeenCalledWith("session-1", "side", expect.any(File), expect.objectContaining({ workspaceRoot: "/side-project" }));
    expect(useAppStore.getState().conversationId).toBe("main");
    expect(useAppStore.getState().draft).toBe("main draft");
    expect(useAppStore.getState().attachments).toEqual([]);
    expect(useAppStore.getState().conversationWorkbenchStates.side).toBeUndefined();
  });

  it("preserves pasted-text metadata and marks the backend attachment payload", async () => {
    mocks.uploadAttachment.mockResolvedValue({
      conversation_id: "conv-upload-1",
      file_name: "pasted-1.txt",
      doc_id: "doc-1",
      artifact_id: "artifact-1",
      attachment: {
        id: "artifact-1",
        file_name: "pasted-1.txt",
        kind: "document",
        media_type: "text/plain",
        artifact_id: "artifact-1",
        data: "large-native-body",
      },
    });
    const file = buildPastedTextFile("长".repeat(20_001));

    uploadComposerFiles([file]);

    expect(useAppStore.getState().attachments[0]).toMatchObject({
      status: "uploading",
      inputSource: "pasted_text",
      sourceCharCount: 20_001,
      localFile: file,
    });
    await waitFor(() => expect(useAppStore.getState().attachments[0].status).toBe("ready"));
    expect(useAppStore.getState().attachments[0].attachment).toMatchObject({
      input_source: "pasted_text",
      source_char_count: 20_001,
    });
    expect(useAppStore.getState().attachments[0].attachment).not.toHaveProperty("data");
    expect(mocks.pushToast).toHaveBeenCalledWith(
      expect.stringContaining("将作为消息内容处理"),
      "info",
      4200,
    );
  });

  it("keeps the original file when disconnected and can retry after reconnecting", async () => {
    mocks.createdOwner = "conv-upload-2";
    mocks.sessionId = null;
    const file = buildPastedTextFile("x".repeat(20_001));
    uploadComposerFiles([file]);
    const failed = useAppStore.getState().attachments[0];

    expect(failed).toMatchObject({ status: "error", localFile: file });

    mocks.sessionId = "session-2";
    mocks.uploadAttachment.mockResolvedValue({
      conversation_id: "conv-upload-2",
      file_name: failed.name,
      doc_id: "doc-2",
      artifact_id: "artifact-2",
      attachment: { file_name: failed.name, kind: "document", artifact_id: "artifact-2" },
    });
    expect(retryComposerAttachment(failed.id)).toBe(true);

    await waitFor(() => expect(useAppStore.getState().attachments[0].status).toBe("ready"));
    expect(mocks.uploadAttachment).toHaveBeenCalledWith(
      "session-2",
      "conv-upload-2",
      file,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("keeps a late first upload in its original workspace after an empty composer switches", async () => {
    mocks.createdOwner = "original-owner";
    let finish!: (value: unknown) => void;
    mocks.uploadAttachment.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    useAppStore.setState({ workingDirectory: "C:\\work\\original" });
    uploadComposerFiles([new File(["body"], "note.txt", { type: "text/plain" })]);
    useAppStore.getState().startConversationDraft({ bindWorkspace: true, workspaceRoot: "C:\\work\\other" });
    await waitFor(() => expect(mocks.uploadAttachment).toHaveBeenCalled());
    finish({
      conversation_id: "original-owner", file_name: "note.txt", doc_id: "doc", artifact_id: "artifact",
      attachment: { file_name: "note.txt", kind: "document", artifact_id: "artifact" },
    });
    await waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates["original-owner"]?.attachments?.[0]?.status).toBe("ready"));
    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.workingDirectory).toBe("C:\\work\\other");
    expect(state.attachments).toEqual([]);
    expect(state.conversations.find((conversation) => conversation.id === "original-owner")?.workspaceRoot?.replaceAll("\\", "/")).toBe("C:/work/original");
    expect(mocks.uploadAttachment).toHaveBeenCalledWith(
      "session-1", "original-owner", expect.any(File), expect.objectContaining({ workspaceRoot: expect.stringMatching(/C:[/\\]work[/\\]original/) }),
    );
  });

  it("keeps late main and side uploads with their owners across actual workspace switches", async () => {
    mocks.createdOwner = "owner-a";
    const finishes: Array<(value: unknown) => void> = [];
    mocks.uploadAttachment.mockImplementation(() => new Promise(resolve => { finishes.push(resolve); }));
    useAppStore.getState().setWorkingDirectory("C:/work/a");
    useAppStore.getState().setDraft("workspace A authored input");
    uploadComposerFiles([new File(["A file"], "a.txt")]);
    await waitFor(() => expect(mocks.uploadAttachment).toHaveBeenCalledOnce());
    const draftB = useAppStore.getState().startConversationDraft({ bindWorkspace: true, workspaceRoot: "C:/work/b" });
    useAppStore.getState().setDraft("workspace B authored input");
    useAppStore.setState({ sideChats: { "side-b": { id: "side-b", workspaceRoot: "C:/work/b", messages: [],
      isStreaming: false, draft: "side B authored input", attachments: [] } } });
    uploadComposerFiles([new File(["side file"], "side.txt")], "side-b");
    finishes[0]({ conversation_id: "owner-a", attachment: { artifact_id: "a-artifact", file_name: "a.txt", kind: "document" } });
    finishes[1]({ conversation_id: "side-b", attachment: { artifact_id: "side-artifact", file_name: "side.txt", kind: "document" } });
    await waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates["owner-a"]?.attachments[0]?.status).toBe("ready"));
    await waitFor(() => expect(useAppStore.getState().sideChats["side-b"].attachments?.[0]?.status).toBe("ready"));
    expect(useAppStore.getState()).toMatchObject({ conversationId: null, workingDirectory: "C:/work/b", draft: "workspace B authored input", attachments: [] });
    expect(useAppStore.getState().conversationWorkbenchStates["owner-a"].draft).toBe("workspace A authored input");
    expect(useAppStore.getState().sideChats["side-b"].draft).toBe("side B authored input");
    expect(mocks.uploadAttachment.mock.calls.map(([, owner, , options]) => [owner, options.workspaceRoot])).toEqual([
      ["owner-a", "C:/work/a"], ["side-b", "C:/work/b"],
    ]);
    await flushComposerDrafts();
    expect((await loadComposerDrafts()).map(record => [record.owner.kind, record.owner.id, record.draft.draft])).toEqual(expect.arrayContaining([
      ["conversation", "owner-a", "workspace A authored input"], ["draft", draftB, "workspace B authored input"],
      ["side", "side-b", "side B authored input"],
    ]));
  });

  it("a backend-confirmed owner deletion aborts its upload and ignores an already arriving result", async () => {
    let finish!: (value: unknown) => void;
    let signal!: AbortSignal;
    mocks.uploadAttachment.mockImplementation((_session, _owner, _file, options) => {
      signal = options.signal;
      return new Promise(resolve => { finish = resolve; });
    });
    useAppStore.setState({ conversationId: "main", draft: "main stays", sideChats: { removed: {
      id: "removed", workspaceRoot: "C:/side", messages: [], isStreaming: false, draft: "deleted input", attachments: [],
    } } });
    uploadComposerFiles([new File(["side file"], "side.txt")], "removed");
    expect(handleCommandResultEvent({ type: "command.result", command: "conversation.delete", level: "success", message: "deleted",
      data: { deleted: true, conversation_id: "removed" } })).toBe(true);
    expect(signal.aborted).toBe(true);
    finish({ conversation_id: "removed", attachment: { artifact_id: "late-file", file_name: "side.txt", kind: "document" } });
    await flushComposerDrafts();
    expect(useAppStore.getState().sideChats.removed).toBeUndefined();
    expect(useAppStore.getState().conversationWorkbenchStates.removed).toBeUndefined();
    expect(useAppStore.getState()).toMatchObject({ conversationId: "main", draft: "main stays", attachments: [] });
    expect((await loadComposerDrafts()).some(record => record.owner.id === "removed")).toBe(false);
  });

  it("does not restore a removed batch attachment when another upload creates the owner", async () => {
    mocks.createdOwner = "batch-owner";
    let finishFirst!: (value: unknown) => void;
    mocks.uploadAttachment.mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }));
    mocks.uploadAttachment.mockResolvedValue({
      conversation_id: "batch-owner", file_name: "second.txt", doc_id: "doc-2", artifact_id: "artifact-2",
      attachment: { file_name: "second.txt", kind: "document", artifact_id: "artifact-2" },
    });
    uploadComposerFiles([new File(["first"], "first.txt"), new File(["second"], "second.txt")]);
    await waitFor(() => expect(mocks.uploadAttachment).toHaveBeenCalledTimes(2));
    const removedId = useAppStore.getState().attachments[0].id;
    cancelComposerUpload(removedId);
    useAppStore.getState().removeAttachment(removedId);
    finishFirst({ conversation_id: "batch-owner" });
    await waitFor(() => expect(useAppStore.getState().attachments[0]?.status).toBe("ready"));
    expect(useAppStore.getState().attachments.map((attachment) => attachment.name)).toEqual(["second.txt"]);
    expect(useAppStore.getState().conversationWorkbenchStates["batch-owner"].attachments?.map((attachment) => attachment.name)).toEqual(["second.txt"]);
  });

  it.each(["remove", "clear"])("does not resurrect ready attachments from the owner cache after %s", async (operation) => {
    mocks.createdOwner = "cached-owner";
    mocks.uploadAttachment.mockResolvedValue({
      conversation_id: "cached-owner", file_name: "note.txt", doc_id: "doc", artifact_id: "artifact",
      attachment: { file_name: "note.txt", kind: "document", artifact_id: "artifact" },
    });
    uploadComposerFiles([new File(["body"], "note.txt")]);
    await waitFor(() => expect(useAppStore.getState().attachments[0]?.status).toBe("ready"));
    const state = useAppStore.getState();
    if (operation === "remove") state.removeAttachment(state.attachments[0].id);
    else state.clearAttachments();
    useAppStore.getState().restoreWorkbenchState("cached-owner");
    expect(useAppStore.getState().attachments).toEqual([]);
  });
});
