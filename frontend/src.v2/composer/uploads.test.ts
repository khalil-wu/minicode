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
import { retryComposerAttachment, uploadComposerFiles } from "./uploads";

describe("composer uploads", () => {
  beforeEach(() => {
    mocks.sessionId = "session-1";
    mocks.uploadAttachment.mockReset();
    mocks.pushToast.mockReset();
    useAppStore.setState({
      conversationId: null,
      conversations: [],
      attachments: [],
      conversationWorkbenchStates: {},
    });
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
});
