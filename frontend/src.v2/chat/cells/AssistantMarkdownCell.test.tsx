/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssistantMarkdownCell } from "./AssistantMarkdownCell";
import type { AssistantMarkdownCellState } from "./cellTypes";
import { useAppStore } from "../../stores";

const { sendMock, openPathMock, revealPathMock, openArtifactPreviewMock, openWorkspaceFilePreviewMock } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  return {
    sendMock: vi.fn(),
    openPathMock: vi.fn(),
    revealPathMock: vi.fn(),
    openArtifactPreviewMock: vi.fn(() => true),
    openWorkspaceFilePreviewMock: vi.fn(() => true),
  };
});

vi.mock("../../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ send: sendMock, sessionId: "session-image-test" }),
}));

vi.mock("../../protocol/ws-outbox", () => ({
  sendClientCommand: sendMock,
}));

vi.mock("../../desktop/runtime", () => ({
  openPath: openPathMock,
  revealPath: revealPathMock,
  isDesktop: () => false,
}));

vi.mock("../openAttachmentPreview", () => ({
  openArtifactPreview: openArtifactPreviewMock,
  openWorkspaceFilePreview: openWorkspaceFilePreviewMock,
}));

vi.mock("../../overlays/DialogService", () => ({
  showConfirm: vi.fn(async () => true),
}));

const originalOpenEditorFile = useAppStore.getState().openEditorFile;
const originalSetRightStackTab = useAppStore.getState().setRightStackTab;

afterEach(() => {
  cleanup();
  sendMock.mockClear();
  openPathMock.mockClear();
  revealPathMock.mockClear();
  openArtifactPreviewMock.mockClear();
  openWorkspaceFilePreviewMock.mockClear();
  useAppStore.setState({
    openEditorFile: originalOpenEditorFile,
    setRightStackTab: originalSetRightStackTab,
    conversationId: null,
    messages: [],
    conversationMessages: {},
    conversationStreaming: {},
    isStreaming: false,
    isConnected: false,
    draft: "",
    quotedMessage: null,
  });
});

const cell = (patch: Partial<AssistantMarkdownCellState>): AssistantMarkdownCellState => ({
  kind: "assistant_markdown",
  id: "assistant-final",
  markdownSource: "",
  phase: "final",
  copyable: false,
  createdAt: 1,
  ...patch,
});

describe("AssistantMarkdownCell sources", () => {
  it("renders streaming markdown immediately from the latest received content", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "北京今天多云。",
          isStreaming: true,
        })}
      />,
    );

    expect(screen.getByText("北京今天多云。")).toBeTruthy();
  });

  it("preserves model-authored bare-domain source text without inventing source chips", () => {
    const { container } = render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "北京今天阴。\n\n数据来源：中央气象台 nmc.cn",
          citations: [{
            source: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
            url: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
            label: "中央气象台",
            range: [0, 0],
          }],
        })}
      />,
    );

    expect(screen.getByText("北京今天阴。")).toBeTruthy();
    expect(document.body.textContent).toContain("数据来源：中央气象台 nmc.cn");
    expect(container.querySelector(".assistant-cell-source-chip")).toBeNull();
  });

  it("renders citation sources in the footer strip", () => {
    const { container } = render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "北京天气参考中央气象台 [1]。",
          citations: [{
            source: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
            url: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
            label: "中央气象台",
            range: [0, 0],
          }],
        })}
      />,
    );

    const link = screen.getByRole("link", { name: "中央气象台" });
    expect(link.getAttribute("href")).toBe("https://www.nmc.cn/publish/forecast/ABJ/beijing.html");
    expect(document.body.textContent).not.toContain("[1]");
    expect(document.querySelector(".assistant-inline-source-chip")).toBeNull();
    expect(document.querySelector(".assistant-cell-source-chip")).toBeTruthy();
    expect(container.querySelector('[data-brand="website"] img')?.getAttribute("src")).toBe(
      "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2Fwww.nmc.cn&sz=64",
    );
  });

  it("renders provider-native sources even when the answer has no numeric markers", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "MiniCode returned a provider-cited answer.",
          citations: [{
            source: "https://provider.example/source",
            url: "https://provider.example/source",
            label: "Provider source",
            range: [0, 0],
            providerNative: true,
          }],
        })}
      />,
    );

    expect(screen.getByRole("link", { name: "Provider source" })).toBeTruthy();
    expect(document.querySelector(".assistant-cell-source-chip")).toBeTruthy();
  });

  it("renders provider-native document locations as informative non-link chips", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "MiniCode returned a document-cited answer.",
          citations: [{
            source: "anthropic:document:abc123",
            title: "Architecture notes",
            label: "Pages 2–3",
            locationType: "page_location",
            range: [2, 3],
            providerNative: true,
          }],
        })}
      />,
    );

    expect(screen.getByRole("note")).toBeTruthy();
    expect(screen.getByText("Pages 2–3")).toBeTruthy();
    expect(screen.queryByRole("link")).toBeNull();
    expect(document.body.textContent).not.toContain("anthropic:document:abc123");
  });

  it("normalizes split host labels in citation source chips", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "北京天气参考中国天气网 [1]。",
          citations: [{
            source: "https://www.weather.com.cn/weather/101010100.shtml",
            url: "https://www.weather.com.cn/weather/101010100.shtml",
            label: "weather.com.c n",
            range: [0, 0],
          }],
        })}
      />,
    );

    expect(screen.getByRole("link", { name: "weather.com.cn" })).toBeTruthy();
    expect(document.body.textContent).not.toContain("weather.com.c n");
  });

  it("opens source chips inside the Browser panel", () => {
    useAppStore.setState({ conversationId: "conv-source-link" });
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "北京天气参考中央气象台 [1]。",
          citations: [{
            source: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
            url: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
            label: "中央气象台",
            range: [0, 0],
          }],
        })}
      />,
    );

    const link = screen.getByRole("link", { name: "中央气象台" });
    expect(link.getAttribute("target")).toBeNull();
    fireEvent.click(link);

    expect(useAppStore.getState().livePreviewUrl).toBeNull();
    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(sendMock).not.toHaveBeenCalledWith({
      type: "preview.navigate",
      url: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
    });
  });

  it("renders multiple citation sources in the footer strip", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "参考 [1] [2] [3] [4] [5]。",
          citations: [
            { source: "https://a.example/source", url: "https://a.example/source", label: "A", range: [0, 0] },
            { source: "https://b.example/source", url: "https://b.example/source", label: "B", range: [0, 0] },
            { source: "https://c.example/source", url: "https://c.example/source", label: "C", range: [0, 0] },
            { source: "https://d.example/source", url: "https://d.example/source", label: "D", range: [0, 0] },
            { source: "https://e.example/source", url: "https://e.example/source", label: "E", range: [0, 0] },
          ],
        })}
      />,
    );

    expect(screen.getByRole("link", { name: /A/ })).toBeTruthy();
    expect(document.body.textContent).not.toContain("[4]");
    expect(document.querySelector(".assistant-inline-source-chip")).toBeNull();
    expect(document.querySelectorAll(".assistant-cell-source-chip")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "再显示 2 个来源" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "再显示 2 个来源" }));

    expect(screen.getByRole("link", { name: /D/ })).toBeTruthy();
    expect(document.querySelectorAll(".assistant-cell-source-chip")).toHaveLength(5);
  });
});

describe("AssistantMarkdownCell image generation", () => {
  const imageProgress = {
    type: "progress" as const,
    id: "provider:image-generation-1",
    stage: "image_generation" as const,
    phase: "image_generation" as const,
    status: "running" as const,
    message: "正在生成图像",
    detail: "Images API 请求已提交",
    timestamp: 1,
  };

  it("renders a full image mask without a rotating spinner while generation is running", () => {
    const { container } = render(
      <AssistantMarkdownCell
        cell={cell({ imageProgress: [imageProgress] })}
      />,
    );

    const mask = screen.getByRole("status");
    expect(mask.getAttribute("data-running")).toBe("true");
    expect(screen.getByText("正在生成图像")).toBeTruthy();
    expect(container.querySelector(".assistant-cell-image-spinner")).toBeNull();
    expect(screen.queryByRole("button", { name: "复制回复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "引用回复" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重新生成" })).toBeNull();
    expect(screen.queryByRole("button", { name: "从此处分支" })).toBeNull();
  });

  it("keeps the mask active after provider completion until the image artifact arrives", () => {
    const { container } = render(
      <AssistantMarkdownCell
        cell={cell({
          imageProgress: [{
            ...imageProgress,
            status: "completed",
            message: "图像生成完成",
          }],
        })}
      />,
    );

    expect(screen.getByRole("status").getAttribute("data-running")).toBe("true");
    expect(screen.getByText("正在载入生成结果")).toBeTruthy();
    expect(container.querySelector(".assistant-cell-image-spinner")).toBeNull();
  });

  it("replaces the mask with the complete live image artifact", () => {
    const dataUrl = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    render(
      <AssistantMarkdownCell
        cell={cell({
          imageProgress: [imageProgress],
          artifacts: [{
            artifactId: "artifact-image-1",
            kind: "image",
            summary: "Generated PNG image",
            mediaType: "image/png",
            bytes: 68,
            url: dataUrl,
          }],
        })}
      />,
    );

    const image = screen.getByRole("img", { name: "模型生成的图片" });
    expect(image.getAttribute("src")).toBe(dataUrl);
    expect(document.body.textContent).not.toContain("Generated PNG image");
    expect(screen.queryByText("正在生成图像")).toBeNull();
    expect(screen.getByRole("status", { name: "正在载入生成图片" })).toBeTruthy();
    fireEvent.load(image);
    expect(screen.queryByRole("status", { name: "正在载入生成图片" })).toBeNull();
    expect(screen.getByRole("button", { name: "查看大图" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制图片" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "保存图片" })).toBeTruthy();
    expect(openArtifactPreviewMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "查看生成图片大图" }));
    expect(screen.getByRole("dialog", { name: "生成图片大图" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭大图" }));
    expect(screen.queryByRole("dialog", { name: "生成图片大图" })).toBeNull();
  });

  it("loads a cold-history generated image inline through the owner-scoped HTTP resource", () => {
    useAppStore.setState({ conversationId: "conv-history-image", isConnected: true });
    render(
      <AssistantMarkdownCell
        conversationId="conv-history-image"
        cell={cell({
          artifacts: [{
            artifactId: "artifact-history-image",
            kind: "image",
            summary: "历史生成图片",
            mediaType: "image/png",
          }],
        })}
      />,
    );

    const image = screen.getByRole("img", { name: "模型生成的图片" });
    const src = image.getAttribute("src") || "";
    expect(src).toContain("/api/artifacts/raw");
    expect(src).toContain("artifact_id=artifact-history-image");
    expect(src).toContain("session_id=session-image-test");
    expect(src).toContain("conversation_id=conv-history-image");
    expect(openArtifactPreviewMock).not.toHaveBeenCalled();

    fireEvent.load(image);
    expect(screen.getByRole("button", { name: "查看大图" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看生成图片大图" }));
    expect(screen.getByRole("dialog", { name: "生成图片大图" })).toBeTruthy();
  });

  it("renders the generated image between the provider intro and completion text", () => {
    const dataUrl = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";
    const { container } = render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "好的，我来生成这张图片。\n\n图像已经为你生成好了。",
          markdownBeforeArtifacts: "好的，我来生成这张图片。",
          markdownAfterArtifacts: "图像已经为你生成好了。",
          imageProgress: [imageProgress],
          artifacts: [{
            artifactId: "artifact-image-order",
            kind: "image",
            summary: "生成图片",
            mediaType: "image/png",
            url: dataUrl,
          }],
        })}
      />,
    );

    const intro = screen.getByText("好的，我来生成这张图片。");
    const image = screen.getByRole("img", { name: "模型生成的图片" });
    const completion = screen.getByText("图像已经为你生成好了。");
    expect(intro.compareDocumentPosition(image) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(image.compareDocumentPosition(completion) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container.querySelector('[data-artifact-id="artifact-image-order"]')).toBeTruthy();
  });
});

describe("AssistantMarkdownCell attachments", () => {
  it("renders a chip per attachment with kind label and size", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "See the artifacts.",
          attachments: [
            { path: "/tmp/shot.png", size: 2048, isImage: true },
            { path: "/tmp/build.log", size: 512, isImage: false },
          ],
        })}
      />,
    );

    expect(screen.getByText("附件")).toBeTruthy();
    expect(screen.getByText("[image]")).toBeTruthy();
    expect(screen.getByText("[file]")).toBeTruthy();
    expect(screen.getByText("shot.png")).toBeTruthy();
    expect(screen.getByText("build.log")).toBeTruthy();
    // Sizes formatted human-readably.
    expect(document.body.textContent).toContain("2.0 KB");
    expect(document.body.textContent).toContain("512 B");
  });

  it("omits the attachments block when there are none", () => {
    render(<AssistantMarkdownCell cell={cell({ markdownSource: "No files." })} />);

    expect(screen.queryByText("附件")).toBeNull();
  });

  it("opens image attachments in the unified Preview panel", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "See the image.",
          attachments: [
            { path: "C:/tmp/shot.png", size: 2048, isImage: true },
          ],
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /shot\.png/ }));

    expect(openWorkspaceFilePreviewMock).toHaveBeenCalledWith(expect.objectContaining({
      path: "C:/tmp/shot.png",
      name: "shot.png",
      mediaType: "image/*",
      kind: "image",
    }));
  });

  it("opens non-image deliverables in the unified Preview panel", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "See the file.",
          attachments: [
            { path: "C:/tmp/build.log", size: 512, isImage: false },
          ],
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /build\.log/ }));

    expect(openWorkspaceFilePreviewMock).toHaveBeenCalledWith(expect.objectContaining({
      path: "C:/tmp/build.log",
      name: "build.log",
      kind: "file",
    }));
    expect(openPathMock).not.toHaveBeenCalled();
  });

  it("opens transcript attachments with the transcript owner instead of the active workspace", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      workingDirectory: "C:/workspace/active",
    });
    render(
      <AssistantMarkdownCell
        conversationId="conv-transcript-owner"
        workspaceRoot="C:/workspace/transcript-owner"
        cell={cell({
          markdownSource: "See the transcript file.",
          attachments: [
            { path: "reports/result.md", size: 512, isImage: false },
          ],
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /result\.md/ }));

    expect(openWorkspaceFilePreviewMock).toHaveBeenCalledWith({
      path: "reports/result.md",
      name: "result.md",
      mediaType: undefined,
      kind: "file",
      workspaceRoot: "C:/workspace/transcript-owner",
      conversationId: "conv-transcript-owner",
    });
  });

  it("does not duplicate an attachment already linked in the model answer", () => {
    render(
      <AssistantMarkdownCell
        cell={cell({
          markdownSource: "文档已创建：[test.docx](C:/Desktop/test.docx)",
          attachments: [
            { path: "C:\\Desktop\\test.docx", size: 1024, isImage: false },
          ],
        })}
      />,
    );

    expect(screen.getByRole("button", { name: "test.docx" })).toBeTruthy();
    expect(screen.queryByText("附件")).toBeNull();
  });
});

describe("AssistantMarkdownCell run cancellation", () => {
  it("forks from the stable assistant message id and includes the legacy index hint", () => {
    useAppStore.setState({
      conversationId: "conv-fork",
      messages: [
        { id: "user-1", role: "user", content: "prompt", artifacts: [], timestamp: 1 },
        { id: "assistant-other", role: "assistant", content: "earlier", artifacts: [], timestamp: 2 },
        { id: "assistant-target", role: "assistant", content: "target", artifacts: [], timestamp: 3 },
      ],
      conversationMessages: {
        "conv-fork": [
          { id: "user-1", role: "user", content: "prompt", artifacts: [], timestamp: 1 },
          { id: "assistant-other", role: "assistant", content: "earlier", artifacts: [], timestamp: 2 },
          { id: "assistant-target", role: "assistant", content: "target", artifacts: [], timestamp: 3 },
        ],
      },
    });

    render(
      <AssistantMarkdownCell
        cell={cell({
          id: "assistant-fork-cell",
          messageId: "assistant-target",
          markdownSource: "target",
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "从此处分支" }));

    expect(sendMock).toHaveBeenCalledWith({
      type: "context.fork",
      message_id: "assistant-target",
      message_index: 2,
      create_branch: true,
      activate: true,
    });
  });

  it("stores quoted replies as composer context without inserting text into the draft", () => {
    useAppStore.setState({ draft: "follow-up", quotedMessage: null });

    render(
      <AssistantMarkdownCell
        cell={cell({
          id: "assistant-quote-cell",
          messageId: "assistant-quote",
          markdownSource: "这里是需要引用的回复。",
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "引用回复" }));

    expect(useAppStore.getState().draft).toBe("follow-up");
    expect(useAppStore.getState().quotedMessage).toMatchObject({
      id: "assistant-quote",
      role: "assistant",
      content: "这里是需要引用的回复。",
    });
  });

  it("does not offer recall on an assistant reply", () => {
    useAppStore.setState({
      conversationId: "conv-assistant-recall",
      messages: [
        { id: "user-1", role: "user", content: "old prompt", artifacts: [], timestamp: 1 },
        { id: "assistant-1", role: "assistant", content: "reply", artifacts: [], timestamp: 2, isStreaming: true },
      ],
      conversationMessages: {
        "conv-assistant-recall": [
          { id: "user-1", role: "user", content: "old prompt", artifacts: [], timestamp: 1 },
          { id: "assistant-1", role: "assistant", content: "reply", artifacts: [], timestamp: 2, isStreaming: true },
        ],
      },
      conversationStreaming: { "conv-assistant-recall": true },
      isStreaming: true,
    });

    render(
      <AssistantMarkdownCell
        cell={cell({
          id: "assistant-cell-1",
          messageId: "assistant-1",
          markdownSource: "reply",
        })}
      />,
    );

    expect(screen.queryByRole("button", { name: "召回到输入框" })).toBeNull();
  });

  it("sends one atomic retry command while the conversation is streaming", async () => {
    sendMock.mockReturnValue(true);
    useAppStore.setState({
      conversationId: "conv-assistant-regenerate",
      messages: [
        { id: "user-2", role: "user", content: "prompt", artifacts: [], timestamp: 1 },
        { id: "assistant-2", role: "assistant", content: "reply", artifacts: [], timestamp: 2, isStreaming: true },
      ],
      conversationMessages: {
        "conv-assistant-regenerate": [
          { id: "user-2", role: "user", content: "prompt", artifacts: [], timestamp: 1 },
          { id: "assistant-2", role: "assistant", content: "reply", artifacts: [], timestamp: 2, isStreaming: true },
        ],
      },
      conversationStreaming: { "conv-assistant-regenerate": true },
      isStreaming: true,
    });

    render(
      <AssistantMarkdownCell
        cell={cell({
          id: "assistant-cell-2",
          messageId: "assistant-2",
          markdownSource: "reply",
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));

    await waitFor(() => expect(sendMock).toHaveBeenCalledWith(expect.objectContaining({
      type: "user_message",
      content: "prompt",
      conversation_id: "conv-assistant-regenerate",
      retry_from_message_id: "user-2",
    })));
    expect(sendMock).not.toHaveBeenCalledWith(expect.objectContaining({ type: "interrupt" }));
  });
});
