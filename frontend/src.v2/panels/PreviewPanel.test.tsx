/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ sessionId: "session-preview-image" }),
}));

import { useAppStore } from "../stores";
import { PreviewPanel } from "./PreviewPanel";

const resetPreviewState = () => {
  useAppStore.setState({
    conversationId: "conv-preview-a",
    livePreviewUrl: "http://localhost:5173",
    previewArtifact: null,
    previewServers: [],
    previewLaunchConfigs: [],
    previewLaunchProcesses: [],
    previewVerification: null,
    workingDirectory: "C:\\Desktop\\MiniCode",
    isConnected: false,
  });
};

describe("PreviewPanel", () => {
  beforeEach(resetPreviewState);
  afterEach(cleanup);

  it("is file-only and does not expose the removed application page", () => {
    render(<PreviewPanel />);
    expect(screen.queryByText("应用")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "预览 URL" })).toBeNull();
    expect(screen.getByText("在对话中打开文件后，可在这里查看完整内容。")).toBeTruthy();
  });

  it("renders markdown attachments in the file preview", () => {
    useAppStore.setState({
      livePreviewUrl: null,
      previewArtifact: {
        artifactId: "notes",
        name: "notes.md",
        mediaType: "text/markdown",
        content: "## Notes\n\nBody",
        source: "attachment",
        loadedAt: Date.now(),
      },
    });
    render(<PreviewPanel />);
    expect(screen.getByText("Notes")).toBeTruthy();
    expect(screen.getByText("Body")).toBeTruthy();
  });

  it("uses the internal PDF viewer for trusted attachment URLs", () => {
    useAppStore.setState({
      livePreviewUrl: null,
      previewArtifact: {
        artifactId: "report",
        name: "report.pdf",
        mediaType: "application/pdf",
        url: "https://assets.example/report.pdf",
        content: "",
      },
    });
    render(<PreviewPanel />);
    expect(screen.getByText("正在准备 PDF 预览")).toBeTruthy();
    expect(document.querySelector('iframe[title="report.pdf"]')).toBeNull();
  });

  it("labels Office previews as extracted content", () => {
    useAppStore.setState({
      livePreviewUrl: null,
      previewArtifact: {
        artifactId: "workbook",
        name: "budget.xlsx",
        mediaType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        kind: "document",
        sizeBytes: 2048,
        content: "## Sheet: Budget\nRevenue | 42",
        source: "attachment",
        loadedAt: Date.now(),
      },
    });
    render(<PreviewPanel />);
    expect(screen.getByText(/Excel · 提取文本/)).toBeTruthy();
    expect(screen.getByText("Sheet: Budget")).toBeTruthy();
  });

  it("does not render SVG data artifacts as images", () => {
    useAppStore.setState({
      livePreviewUrl: null,
      previewArtifact: {
        artifactId: "svg",
        name: "unsafe.svg",
        mediaType: "image/svg+xml",
        url: "data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+",
        content: "<svg onload=alert(1)>fallback</svg>",
      },
    });
    render(<PreviewPanel />);
    expect(screen.queryByRole("img", { name: "unsafe.svg" })).toBeNull();
    expect(screen.getByText(/fallback/)).toBeTruthy();
  });

  it("shows image load failures and offers a retry", () => {
    useAppStore.setState({
      livePreviewUrl: null,
      previewArtifact: {
        artifactId: "broken-image",
        name: "screenshot.png",
        mediaType: "IMAGE/PNG; charset=binary",
        url: "https://assets.example/broken.png",
        content: "",
      },
    });
    render(<PreviewPanel />);
    const image = screen.getByRole("img", { name: "screenshot.png" });
    fireEvent.error(image);

    expect(screen.getByText("图片加载失败。")).toBeTruthy();
    const retry = screen.getByRole("button", { name: "重试图片预览" });
    fireEvent.click(retry);
    const retried = screen.getByRole("img", { name: "screenshot.png" }) as HTMLImageElement;
    expect(retried.src).toContain("preview_retry=1");
  });

  it("rebuilds an owner-scoped image after reconnect instead of preserving a stale fetch error", () => {
    useAppStore.setState({
      isConnected: false,
      previewArtifact: {
        artifactId: "persisted-screenshot",
        name: "browser-screenshot.png",
        mediaType: "image/png",
        content: "",
        source: "artifact",
        loading: true,
        error: "旧连接中的附件请求失败",
      },
    });
    render(<PreviewPanel />);

    expect(screen.getByText("连接恢复后可预览图片。")).toBeTruthy();
    expect(screen.queryByText("旧连接中的附件请求失败")).toBeNull();
    expect(screen.queryByText("正在加载附件预览")).toBeNull();

    act(() => {
      useAppStore.setState({ isConnected: true });
    });

    const image = screen.getByRole("img", { name: "browser-screenshot.png" });
    const src = image.getAttribute("src") || "";
    expect(src).toContain("/api/artifacts/raw");
    expect(src).toContain("artifact_id=persisted-screenshot");
    expect(src).toContain("session_id=session-preview-image");
    expect(src).toContain("conversation_id=conv-preview-a");
  });

  it("keeps the signed image URL stable across an unrelated component rerender", () => {
    useAppStore.setState({
      isConnected: true,
      previewArtifact: {
        artifactId: "stable-screenshot",
        name: "stable-screenshot.png",
        mediaType: "image/png",
        content: "",
        source: "artifact",
      },
    });
    const view = render(<PreviewPanel />);
    const first = screen.getByRole("img", { name: "stable-screenshot.png" }).getAttribute("src");

    view.rerender(<PreviewPanel />);

    expect(screen.getByRole("img", { name: "stable-screenshot.png" }).getAttribute("src")).toBe(first);
  });

  it("renders legacy image artifacts when their persisted MIME type is missing", () => {
    useAppStore.setState({
      isConnected: true,
      previewArtifact: {
        artifactId: "legacy-browser-screenshot",
        name: "legacy-screenshot.png",
        kind: "image",
        content: "",
        source: "artifact",
        loadedAt: Date.now(),
      },
    });

    render(<PreviewPanel />);

    const image = screen.getByRole("img", { name: "legacy-screenshot.png" });
    expect(image.getAttribute("src")).toContain("artifact_id=legacy-browser-screenshot");
  });
});
