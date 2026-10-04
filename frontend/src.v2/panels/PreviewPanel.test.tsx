/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ sessionId: "session-preview-image" }),
}));
vi.hoisted(() => {
  (globalThis as unknown as { __MINICODE_RUNTIME__: object }).__MINICODE_RUNTIME__ = { runtimeToken: "test-preview-runtime-token" };
});
vi.mock("./PdfAttachmentPreview", () => ({ PdfAttachmentPreview: ({ url, name, onRetry }: { url: string; name: string; onRetry?: () => void }) => <div data-testid="pdf-resource" data-url={url} aria-label={`PDF 预览 ${name}`}>
  {onRetry && <button onClick={onRetry}>重试 PDF 预览</button>}
</div> }));

const downloadMocks = vi.hoisted(() => ({ original: vi.fn(), artifact: vi.fn(), toast: vi.fn(), clipboard: vi.fn() }));
vi.mock("../protocol/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../protocol/api")>(),
  fetchAttachmentOriginal: downloadMocks.original,
  fetchArtifactOriginal: downloadMocks.artifact,
}));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: downloadMocks.toast }));

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
    conversationWorkbenchStates: {},
    previewOwnerConversationId: null,
  });
  downloadMocks.original.mockReset();
  downloadMocks.artifact.mockReset();
  downloadMocks.toast.mockReset();
  downloadMocks.clipboard.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: downloadMocks.clipboard } });
};

describe("PreviewPanel", () => {
  it("plays owner-scoped audio without base64 and rebuilds its URL after reconnect", () => {
    useAppStore.setState({ conversationId: "conv-audio", isConnected: true,
      previewArtifact: { artifactId: "art-audio", name: "voice.wav", content: "", mediaType: "audio/wav", source: "artifact" } });
    render(<PreviewPanel />);
    const player = screen.getByLabelText("voice.wav") as HTMLAudioElement;
    expect(player.controls).toBe(true);
    expect(player.autoplay).toBe(false);
    expect(player.src).toContain("/api/artifacts/raw");
    expect(player.src).toContain("conversation_id=conv-audio");
    expect(player.src).not.toContain("data:");
    act(() => useAppStore.setState({ isConnected: false }));
    expect(document.querySelector("audio")).toBeNull();
    act(() => useAppStore.setState({ isConnected: true }));
    expect(document.querySelector("audio")).not.toBeNull();
    fireEvent.error(screen.getByLabelText("voice.wav"));
    expect(screen.getByText("此音频无法播放，请下载后使用本地播放器打开。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "下载音频" })).toBeTruthy();
  });
  beforeEach(resetPreviewState);
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

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

  it("uses the internal PDF viewer for trusted document URLs", async () => {
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
    expect((await screen.findByTestId("pdf-resource")).getAttribute("data-url")).toBe("https://assets.example/report.pdf");
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

  it("renders an SVG served by the owner-scoped workspace resource", () => {
    useAppStore.setState({
      livePreviewUrl: null,
      previewArtifact: {
        artifactId: "workspace:assets/diagram.svg",
        name: "diagram.svg",
        mediaType: "image/svg+xml",
        kind: "code",
        source: "workspace",
        url: "http://127.0.0.1:8000/api/workspace/raw?path=assets%2Fdiagram.svg&workspace_root=C%3A%2Fowner",
        content: "<svg viewBox=\"0 0 10 10\"></svg>",
        loadedAt: Date.now(),
      },
    });

    expect(render(<PreviewPanel />).getByRole("img", { name: "diagram.svg" })).toBeTruthy();
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

  it.each(["artifact", "attachment"] as const)("rebuilds a restored %s PDF with its actual preview owner after reconnect", async (source) => {
    useAppStore.setState({ previewOwnerConversationId: "pdf-owner", conversationWorkbenchStates: {
      "pdf-owner": { previewArtifact: { artifactId: "pdf-one", name: "report.pdf", mediaType: "application/pdf", content: "", source, hasNative: true,
        url: "http://old-host/api/attachments/raw?session_id=old-session", error: "Old transport failed" } } as never,
    } });
    render(<PreviewPanel />);
    expect(screen.getByText("连接恢复并关联会话后可预览 PDF。")).toBeTruthy();
    expect(screen.queryByText("Old transport failed")).toBeNull();
    act(() => useAppStore.setState({ isConnected: true }));
    const resource = new URL((await screen.findByTestId("pdf-resource")).getAttribute("data-url")!);
    expect(resource.searchParams.get("session_id")).toBe("session-preview-image");
    expect(resource.searchParams.get("conversation_id")).toBe("pdf-owner");
    expect(resource.searchParams.get("artifact_id")).toBe("pdf-one");
    expect(resource.hostname).not.toBe("old-host");
  });

  it.each(["image", "pdf"])("signs a new owner-scoped %s token after explicit retry past the actual five-minute expiry", async (kind) => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    useAppStore.setState({ isConnected: true, previewArtifact: { artifactId: "signed", name: `signed.${kind === "pdf" ? "pdf" : "png"}`, mediaType: kind === "pdf" ? "application/pdf" : "image/png", content: "", source: "artifact" } });
    render(<PreviewPanel />);
    const resourceUrl = () => kind === "pdf" ? screen.getByTestId("pdf-resource").getAttribute("data-url")! : screen.getByRole("img").getAttribute("src")!;
    if (kind === "pdf") await screen.findByTestId("pdf-resource");
    const before = new URL(resourceUrl());
    expect(before.searchParams.get("asset_token")!.split(".")[0]).toBe("1300");
    if (kind === "image") fireEvent.error(screen.getByRole("img"));
    clock.mockReturnValue(1_301_000);
    fireEvent.click(screen.getByRole("button", { name: `重试${kind === "pdf" ? " PDF " : "图片"}预览` }));
    const after = new URL(resourceUrl());
    expect(after.searchParams.get("asset_token")!.split(".")[0]).toBe("1601");
    expect(after.searchParams.get("preview_retry")).toBe("1");
    expect(after.searchParams.get("conversation_id")).toBe("conv-preview-a");
  });

  it.each(["image", "pdf"])("preserves the original workspace when retrying an expired %s resource after the active workspace changes", async (kind) => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    useAppStore.setState({ workingDirectory: "C:/active-other", previewArtifact: { artifactId: "workspace:owner-file", name: `owner.${kind === "pdf" ? "pdf" : "png"}`, mediaType: kind === "pdf" ? "application/pdf" : "image/png", content: "", source: "workspace",
      url: "http://old-host/api/workspace/raw?path=assets%2Fowner-file&workspace_root=C%3A%2Fowner&raw_token=expired" } });
    render(<PreviewPanel />);
    if (kind === "pdf") await screen.findByTestId("pdf-resource");
    if (kind === "image") fireEvent.error(screen.getByRole("img"));
    clock.mockReturnValue(1_301_000);
    fireEvent.click(screen.getByRole("button", { name: `重试${kind === "pdf" ? " PDF " : "图片"}预览` }));
    const after = new URL(kind === "pdf" ? screen.getByTestId("pdf-resource").getAttribute("data-url")! : screen.getByRole("img").getAttribute("src")!);
    expect(after.searchParams.get("workspace_root")).toBe("C:/owner");
    expect(after.searchParams.get("path")).toBe("assets/owner-file");
    expect(after.searchParams.get("raw_token")!.split(".")[0]).toBe("1601");
    expect(after.searchParams.get("preview_retry")).toBe("1");
  });

  it("reports a clipboard refusal and retries the exact unformatted source", async () => {
    const content = '{"value":1}';
    downloadMocks.clipboard.mockRejectedValueOnce(new Error("Clipboard access denied"));
    useAppStore.setState({ previewArtifact: { artifactId: "text", content, mediaType: "application/json" } });
    render(<PreviewPanel />);
    fireEvent.click(screen.getByRole("button", { name: "复制文件内容" }));
    await waitFor(() => expect(downloadMocks.toast).toHaveBeenCalledWith("复制文件内容失败：Clipboard access denied", "error", 3000));
    fireEvent.click(screen.getByRole("button", { name: "复制文件内容" }));
    await waitFor(() => expect(downloadMocks.toast).toHaveBeenCalledWith("文件内容已复制。", "success", 1600));
    expect(downloadMocks.clipboard.mock.calls).toEqual([[content], [content]]);
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

  it("downloads original bytes using the preview conversation owner", async () => {
    const original = new Blob(["original Word bytes"], { type: "application/octet-stream" });
    let resolveDownload!: (blob: Blob) => void;
    downloadMocks.original.mockReturnValue(new Promise<Blob>((resolve) => { resolveDownload = resolve; }));
    const createObjectURL = vi.fn(() => "blob:original-document");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
    const clicks: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () { clicks.push(this.download); });
    useAppStore.setState({
      previewOwnerConversationId: "conv-preview-owner",
      conversationWorkbenchStates: {
        "conv-preview-owner": {
          previewArtifact: { artifactId: "document", name: "report.docx", content: "Extracted text", source: "attachment", hasNative: true, loadedAt: 1 },
        } as never,
      },
    });
    render(<PreviewPanel />);
    const button = screen.getByRole("button", { name: "下载原文件" }) as HTMLButtonElement;
    fireEvent.click(button);
    expect(button.disabled).toBe(true);
    expect(downloadMocks.original).toHaveBeenCalledWith("session-preview-image", "conv-preview-owner", "document");

    await act(async () => resolveDownload(original));

    expect(createObjectURL).toHaveBeenCalledWith(original);
    expect(clicks).toEqual(["report.docx"]);
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:original-document"));
    expect(button.disabled).toBe(false);
  });

  it("downloads an MCP binary artifact from the artifact endpoint", async () => {
    const original = new Blob([new Uint8Array([1, 2, 3])], { type: "application/octet-stream" });
    downloadMocks.artifact.mockResolvedValue(original);
    vi.stubGlobal("URL", Object.assign(class extends URL {}, {
      createObjectURL: vi.fn(() => "blob:mcp-resource"),
      revokeObjectURL: vi.fn(),
    }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    useAppStore.setState({
      conversationId: "conv-mcp",
      isConnected: true,
      previewArtifact: {
        artifactId: "art-mcp", name: "mcp-resource", content: "",
        kind: "binary", mediaType: "application/octet-stream", source: "artifact",
        url: "http://127.0.0.1:8100/api/artifacts/raw?artifact_id=art-mcp",
      },
    });
    render(<PreviewPanel />);
    fireEvent.click(screen.getByRole("button", { name: "下载原文件" }));
    await waitFor(() => expect(downloadMocks.artifact).toHaveBeenCalledWith(
      "session-preview-image", "conv-mcp", "art-mcp",
    ));
    expect(screen.getByText("不支持应用内预览。此文件是二进制文件，无法提取可显示文本。")).toBeTruthy();
  });

  it("surfaces download errors and leaves the original available for retry", async () => {
    downloadMocks.original.mockRejectedValueOnce(new Error("Original file unavailable"));
    useAppStore.setState({ previewArtifact: { artifactId: "document", name: "report.docx", content: "Extracted text", source: "attachment", hasNative: true, loadedAt: 1 } });
    render(<PreviewPanel />);
    fireEvent.click(screen.getByRole("button", { name: "下载原文件" }));
    await waitFor(() => expect(downloadMocks.toast).toHaveBeenCalledWith("Original file unavailable", "error"));
    expect((screen.getByRole("button", { name: "下载原文件" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
