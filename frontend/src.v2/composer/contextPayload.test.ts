/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from "vitest";
import { buildContextNativeAttachments, buildContextPayload } from "./contextPayload";

const mocks = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  fsListTree: vi.fn(),
  isDesktop: vi.fn(() => false),
  listWorkspaceTree: vi.fn(),
  readWorkspaceFile: vi.fn(),
}));

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://127.0.0.1:8787",
  authHeaders: () => ({ "X-Test": "1" }),
  fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init),
  uploadAttachment: mocks.uploadAttachment,
}));

vi.mock("../desktop/runtime", () => ({
  fsListTree: mocks.fsListTree,
  fsReadFileInfo: vi.fn(),
  isDesktop: mocks.isDesktop,
}));

vi.mock("../protocol/workspace", () => ({
  listWorkspaceTree: mocks.listWorkspaceTree,
  readWorkspaceFile: mocks.readWorkspaceFile,
}));

describe("contextPayload native attachments", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("keeps image file context as a native attachment marker instead of text extraction", async () => {
    const payload = await buildContextPayload([
      { kind: "file", path: "docs/problem.png", name: "problem.png" },
    ]);

    expect(payload).toContain("File reference: docs/problem.png");
    expect(mocks.readWorkspaceFile).not.toHaveBeenCalled();
  });

  it("uploads referenced images as native turn attachments", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["png"], { type: "image/png" }),
    } as Response);
    mocks.uploadAttachment.mockResolvedValue({
      conversation_id: "conv-context",
      artifact_id: "art_img",
      doc_id: "doc_img",
      attachment: {
        id: "att_img",
        kind: "image",
        file_name: "problem.png",
        media_type: "image/png",
        artifact_id: "art_img",
        doc_id: "doc_img",
        size_bytes: 3,
      },
    });

    const native = await buildContextNativeAttachments([
      { kind: "file", path: "docs/problem.png", name: "problem.png" },
    ], "session-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8787/api/workspace/raw?path=docs%2Fproblem.png",
      { headers: { "X-Test": "1" } },
    );
    expect(mocks.uploadAttachment).toHaveBeenCalledOnce();
    expect(native.attachments).toHaveLength(1);
    expect(native.attachmentRefs[0]).toMatchObject({
      name: "problem.png",
      kind: "image",
      mediaType: "image/png",
      artifactId: "art_img",
    });
  });

  it("collects native PDF files from selected folders", async () => {
    mocks.listWorkspaceTree.mockResolvedValue({
      name: "docs",
      path: "docs",
      is_dir: true,
      children: [
        { name: "paper.pdf", path: "docs/paper.pdf", is_dir: false, size_bytes: 100 },
        { name: "notes.md", path: "docs/notes.md", is_dir: false, size_bytes: 80 },
      ],
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["pdf"], { type: "application/pdf" }),
    } as Response);
    mocks.uploadAttachment.mockResolvedValue({
      conversation_id: "conv-context",
      artifact_id: "art_pdf",
      doc_id: "doc_pdf",
      attachment: {
        id: "att_pdf",
        kind: "document",
        file_name: "paper.pdf",
        media_type: "application/pdf",
        artifact_id: "art_pdf",
        doc_id: "doc_pdf",
        size_bytes: 3,
      },
    });

    const native = await buildContextNativeAttachments([
      { kind: "folder", path: "docs", name: "docs" },
    ], "session-1");

    expect(mocks.uploadAttachment).toHaveBeenCalledOnce();
    expect(native.attachmentRefs[0]).toMatchObject({
      name: "paper.pdf",
      kind: "document",
      mediaType: "application/pdf",
    });
  });

  it("rejects workspace media above Claude Code's safe source limits before fetching", async () => {
    mocks.listWorkspaceTree.mockResolvedValue({
      name: "media",
      path: "media",
      is_dir: true,
      children: [
        { name: "large.png", path: "media/large.png", is_dir: false, size_bytes: 21 * 1024 * 1024 },
        { name: "large.pdf", path: "media/large.pdf", is_dir: false, size_bytes: 21 * 1024 * 1024 },
      ],
    });
    const fetchMock = vi.spyOn(globalThis, "fetch");

    const native = await buildContextNativeAttachments([
      { kind: "folder", path: "media", name: "media" },
    ], "session-1");

    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.uploadAttachment).not.toHaveBeenCalled();
    expect(native.attachments).toEqual([]);
    expect(native.notes).toContain("large.png");
    expect(native.notes).toContain("large.pdf");
  });

  it("caps native workspace media at the provider's 100-item request limit", async () => {
    mocks.listWorkspaceTree.mockResolvedValue({
      name: "media",
      path: "media",
      is_dir: true,
      children: Array.from({ length: 101 }, (_, index) => ({
        name: `image-${index}.png`,
        path: `media/image-${index}.png`,
        is_dir: false,
        size_bytes: 1,
      })),
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["x"], { type: "image/png" }),
    } as Response);
    mocks.uploadAttachment.mockImplementation(async (_sessionId, conversationId, file: File) => ({
      conversation_id: conversationId || "conv-context",
      artifact_id: `artifact-${file.name}`,
      doc_id: `doc-${file.name}`,
      attachment: {
        id: `attachment-${file.name}`,
        kind: "image",
        file_name: file.name,
        media_type: "image/png",
        artifact_id: `artifact-${file.name}`,
        doc_id: `doc-${file.name}`,
        size_bytes: 1,
      },
    }));

    const native = await buildContextNativeAttachments([
      { kind: "folder", path: "media", name: "media" },
    ], "session-1");

    expect(native.attachments).toHaveLength(100);
    expect(mocks.uploadAttachment).toHaveBeenCalledTimes(100);
    expect(native.notes).toContain("capped at 100");
  });
});
