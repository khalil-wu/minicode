/* @vitest-environment jsdom */

import { waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  sessionId: "session-1" as string | null,
  uploadAttachment: vi.fn(),
  pushToast: vi.fn(),
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
  getWebSocket: () => mocks.sessionId ? { sessionId: mocks.sessionId } : null,
}));

vi.mock("../protocol/api", () => ({
  uploadAttachment: mocks.uploadAttachment,
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: mocks.pushToast,
}));

import { useAppStore } from "../stores";
import { buildPastedTextFile } from "./pastedText";
import { cancelComposerUpload, retryComposerAttachment, uploadComposerFiles } from "./uploads";

describe("composer uploads", () => {
  beforeEach(() => {
    mocks.sessionId = "session-1";
    mocks.uploadAttachment.mockReset();
    mocks.pushToast.mockReset();
    useAppStore.setState({
      conversationId: null,
      conversations: [],
      attachments: [],
      workingDirectory: "",
      conversationWorkbenchStates: {},
      sideChats: {},
    });
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
      "",
      file,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("keeps a late first upload in its original workspace after an empty composer switches", async () => {
    let finish!: (value: unknown) => void;
    mocks.uploadAttachment.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    useAppStore.setState({ workingDirectory: "C:\\work\\original" });
    uploadComposerFiles([new File(["body"], "note.txt", { type: "text/plain" })]);
    useAppStore.setState({ workingDirectory: "C:\\work\\other" });
    finish({
      conversation_id: "original-owner", file_name: "note.txt", doc_id: "doc", artifact_id: "artifact",
      attachment: { file_name: "note.txt", kind: "document", artifact_id: "artifact" },
    });
    await waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates["original-owner"]?.attachments?.[0]?.status).toBe("ready"));
    const state = useAppStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.workingDirectory).toBe("C:\\work\\other");
    expect(state.attachments).toEqual([]);
    expect(state.conversations.find((conversation) => conversation.id === "original-owner")?.workspaceRoot).toBe("C:\\work\\original");
    expect(mocks.uploadAttachment).toHaveBeenCalledWith(
      "session-1", "", expect.any(File), expect.objectContaining({ workspaceRoot: "C:\\work\\original" }),
    );
  });

  it("does not restore a removed batch attachment when another upload creates the owner", async () => {
    let finishFirst!: (value: unknown) => void;
    mocks.uploadAttachment.mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }));
    mocks.uploadAttachment.mockResolvedValue({
      conversation_id: "batch-owner", file_name: "second.txt", doc_id: "doc-2", artifact_id: "artifact-2",
      attachment: { file_name: "second.txt", kind: "document", artifact_id: "artifact-2" },
    });
    uploadComposerFiles([new File(["first"], "first.txt"), new File(["second"], "second.txt")]);
    const removedId = useAppStore.getState().attachments[0].id;
    cancelComposerUpload(removedId);
    useAppStore.getState().removeAttachment(removedId);
    finishFirst({ conversation_id: "batch-owner" });
    await waitFor(() => expect(useAppStore.getState().attachments[0]?.status).toBe("ready"));
    expect(useAppStore.getState().attachments.map((attachment) => attachment.name)).toEqual(["second.txt"]);
    expect(useAppStore.getState().conversationWorkbenchStates["batch-owner"].attachments?.map((attachment) => attachment.name)).toEqual(["second.txt"]);
  });

  it.each(["remove", "clear"])("does not resurrect ready attachments from the owner cache after %s", async (operation) => {
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
