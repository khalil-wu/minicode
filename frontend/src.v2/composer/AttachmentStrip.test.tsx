/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { AttachmentStrip } from "./AttachmentStrip";

const uploadMocks = vi.hoisted(() => ({
  cancelComposerUpload: vi.fn(),
  retryComposerAttachment: vi.fn(() => true),
  openAttachmentPreview: vi.fn(() => true),
  openLocalFilePreview: vi.fn(() => true),
}));

vi.mock("./uploads", () => uploadMocks);
vi.mock("../chat/openAttachmentPreview", () => ({
  openAttachmentPreview: uploadMocks.openAttachmentPreview,
  openLocalFilePreview: uploadMocks.openLocalFilePreview,
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

describe("AttachmentStrip", () => {
  beforeEach(() => {
    uploadMocks.cancelComposerUpload.mockClear();
    uploadMocks.retryComposerAttachment.mockClear();
    uploadMocks.openAttachmentPreview.mockClear();
    uploadMocks.openLocalFilePreview.mockClear();
    useAppStore.setState({ attachments: [] });
  });

  afterEach(() => {
    cleanup();
  });

  it("shows failed image uploads directly on the chip", () => {
    useAppStore.setState({
      attachments: [{
        id: "image-failed",
        name: "screen.png",
        type: "image/png",
        size: 2048,
        status: "error",
        dataUrl: "data:image/png;base64,AA==",
        error: "Image upload failed",
      }],
    });

    render(<AttachmentStrip />);

    fireEvent.click(screen.getByRole("button", { name: "预览 screen.png" }));
    expect(uploadMocks.openLocalFilePreview).toHaveBeenCalledWith(expect.objectContaining({
      id: "image-failed",
      name: "screen.png",
      mediaType: "image/png",
      url: "data:image/png;base64,AA==",
    }));

    expect(screen.getByLabelText("screen.png 上传失败")).toBeTruthy();
    expect(screen.queryByText("failed")).toBeNull();
    expect(screen.getAllByTitle("Image upload failed")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "移除 screen.png" }));
    expect(useAppStore.getState().attachments).toHaveLength(0);
  });

  it("keeps document upload warnings visible without blocking the ready status", () => {
    useAppStore.setState({
      attachments: [{
        id: "pdf-warning",
        name: "report.pdf",
        type: "application/pdf",
        size: 4096,
        status: "ready",
        artifactId: "artifact-pdf",
        error: "PDF attached; extracted text is not indexed.",
        attachment: { id: "artifact-pdf", kind: "document" },
      }],
    });

    render(<AttachmentStrip />);

    expect(screen.queryByText("warning")).toBeNull();
    expect(screen.getByText("PDF attached; extracted text is not indexed.")).toBeTruthy();
  });

  it("keeps ready document chips focused on the filename", () => {
    useAppStore.setState({
      attachments: [{
        id: "doc-ready",
        name: "requirements.md",
        type: "text/markdown",
        size: 8192,
        status: "ready",
        artifactId: "artifact-doc",
        docId: "doc-1",
      }],
    });

    render(<AttachmentStrip />);

    expect(screen.getByText("requirements.md")).toBeTruthy();
    expect(screen.queryByText(/KB|chunks|text ready|stored|ready/i)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "预览 requirements.md" }));
    expect(uploadMocks.openAttachmentPreview).toHaveBeenCalledWith({
      artifactId: "artifact-doc",
      name: "requirements.md",
      mediaType: "text/markdown",
      kind: "document",
    });
  });

  it("shows pasted-text size and lets a failed upload retry without losing the source", () => {
    const localFile = new File(["long text"], "pasted-7.txt", { type: "text/plain" });
    useAppStore.setState({
      attachments: [{
        id: "pasted-failed",
        name: "pasted-7.txt",
        type: "text/plain",
        size: localFile.size,
        status: "error",
        error: "Session disconnected",
        inputSource: "pasted_text",
        sourceCharCount: 20_001,
        localFile,
      }],
    });

    render(<AttachmentStrip />);

    expect(screen.getByText("20,001 chars")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新上传 pasted-7.txt" }));
    expect(uploadMocks.retryComposerAttachment).toHaveBeenCalledWith("pasted-failed");

    fireEvent.click(screen.getByRole("button", { name: "移除 pasted-7.txt" }));
    expect(uploadMocks.cancelComposerUpload).toHaveBeenCalledWith("pasted-failed");
    expect(useAppStore.getState().attachments).toHaveLength(0);
  });
});
