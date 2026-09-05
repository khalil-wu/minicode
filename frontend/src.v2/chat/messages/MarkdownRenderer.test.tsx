/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { findStableSplitPoint, MarkdownRenderer } from "./MarkdownRenderer";
import { useAppStore } from "../../stores";
import { __resetOpenWebInPreviewDedupeForTests } from "../openWebInPreview";
import { registerWebSocketSender } from "../../protocol/ws-outbox";
import {
  __resetOpenWebInBrowserForTests,
  subscribeBrowserOpenRequests,
} from "../openWebInBrowser";
import mermaid from "mermaid";

const { sendMock, openPathMock, revealPathMock } = vi.hoisted(() => {
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
  return { sendMock: vi.fn(), openPathMock: vi.fn(), revealPathMock: vi.fn() };
});

vi.mock("../../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ send: sendMock }),
}));

vi.mock("../../desktop/runtime", () => ({
  openPath: openPathMock,
  revealPath: revealPathMock,
  // These specs exercise the desktop shell path (default-app open / reveal),
  // which is only offered when a desktop runtime is present.
  isDesktop: () => true,
}));

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (id: string) => ({
      svg: `<svg id="${id}" role="img"><text>Rendered Mermaid</text></svg>`,
    })),
  },
}));

afterEach(() => {
  cleanup();
  registerWebSocketSender(null);
  sendMock.mockClear();
  openPathMock.mockClear();
  revealPathMock.mockClear();
  __resetOpenWebInPreviewDedupeForTests();
  __resetOpenWebInBrowserForTests();
  useAppStore.setState({
    conversationId: null,
    remoteImagePolicy: "ask",
    allowedRemoteImageDomains: [],
    previewArtifact: null,
  });
});

describe("MarkdownRenderer", () => {
  it("uses the plain streaming code path when a fence has no preceding paragraph", () => {
    const { container, rerender } = render(<MarkdownRenderer content={"```ts\nconst value = 1;"} isStreaming />);
    const pre = container.querySelector("pre");
    expect(pre).toBeTruthy();
    expect(pre?.textContent).toContain("const value = 1;");
    expect(container.querySelector(".token")).toBeNull();
    rerender(<MarkdownRenderer content={"```ts\nconst value = 1;\nconst next = 2;"} isStreaming />);
    expect(container.querySelector("pre")).toBe(pre);
    expect(pre?.textContent).toContain("const next = 2;");
  });

  it("keeps rendered nodes mounted while streamed text grows", () => {
    // The first line flickered because appending a delta rebuilt the component
    // table, which remounts every element React had already committed. Identity
    // of an already-rendered node is the observable contract here.
    const { container, rerender } = render(
      <MarkdownRenderer content={"## 标题\n\n第一行"} isStreaming />,
    );
    const heading = container.querySelector("h2");
    const headingId = heading?.getAttribute("id");
    expect(heading).toBeTruthy();
    expect(headingId).toBeTruthy();

    rerender(<MarkdownRenderer content={"## 标题\n\n第一行，继续写下去"} isStreaming />);

    expect(container.querySelector("h2")).toBe(heading);
    expect(container.querySelector("h2")?.getAttribute("id")).toBe(headingId);
  });

  it("gives repeated heading text stable distinct anchors across re-renders", () => {
    const content = "## 概览\n\n一段文字\n\n## 概览\n\n另一段文字";
    const { container, rerender } = render(<MarkdownRenderer content={content} />);
    const ids = [...container.querySelectorAll("h2")].map((node) => node.getAttribute("id"));

    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);

    rerender(<MarkdownRenderer content={content} />);

    expect([...container.querySelectorAll("h2")].map((node) => node.getAttribute("id"))).toEqual(ids);
  });

  it("keeps split stable and streaming heading IDs unique when only the tail rerenders", () => {
    const prefix = "A".repeat(130);
    const first = `${prefix}\n\n## Duplicate\n\nStable body\n\n## Duplicate`;
    const second = `${first}\n<!-- tail update -->`;
    const { container, rerender } = render(<MarkdownRenderer content={first} isStreaming />);
    const firstHeadings = [...container.querySelectorAll("h2")];
    const firstIds = firstHeadings.map((node) => node.getAttribute("id"));
    expect(firstIds).toHaveLength(2);
    expect(new Set(firstIds).size).toBe(2);

    rerender(<MarkdownRenderer content={second} isStreaming />);

    const secondIds = [...container.querySelectorAll("h2")].map((node) => node.getAttribute("id"));
    expect(new Set(secondIds).size).toBe(2);
    expect(secondIds).toEqual(firstIds);
    expect(container.querySelectorAll("h2")[0]).toBe(firstHeadings[0]);
  });

  it("does not reuse a stable heading ID when a heading-only tail rerenders", () => {
    const prefix = `${"A".repeat(220)}\n\n## Duplicate\n\nStable body\n\n`;
    const first = `${prefix}## Duplicate`;
    const second = `${prefix}## **Duplicate**`;
    const { container, rerender } = render(<MarkdownRenderer content={first} isStreaming />);
    const firstIds = [...container.querySelectorAll("h2")].map((node) => node.getAttribute("id"));

    expect(firstIds).toHaveLength(2);
    expect(new Set(firstIds).size).toBe(2);

    rerender(<MarkdownRenderer content={second} isStreaming />);

    const secondIds = [...container.querySelectorAll("h2")].map((node) => node.getAttribute("id"));
    expect(secondIds).toEqual(firstIds);
    expect(new Set(secondIds).size).toBe(2);
  });

  it("keeps malformed percent-encoded heading links renderable", () => {
    expect(() => render(
      <MarkdownRenderer content={["[Jump](#broken%fragment)", "", "## Broken%fragment"].join("\n")} />,
    )).not.toThrow();

    expect(screen.getByRole("heading", { name: "Broken%fragment" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Jump" }).getAttribute("href")).toContain("brokenfragment");
  });

  it("never promotes blank lines inside an unclosed fenced block into the stable prefix", () => {
    const prefix = `${"A".repeat(140)}\n\n`;
    const content = `${prefix}\`\`\`ts\nconst first = 1;\n\nconst second = 2;\n`;

    expect(findStableSplitPoint(content)).toBe(prefix.length);
    expect(findStableSplitPoint(`${"x".repeat(120)}\n\`\`\`ts\nline one\n\nline two`)).toBe(0);
  });

  it("promotes a fenced block only after the closing fence arrives", () => {
    const prefix = `${"A".repeat(140)}\n\n`;
    const open = `${prefix}\`\`\`ts\nconst value = 1;\n\nconst next = 2;`;
    const closed = `${open}\n\`\`\``;

    expect(findStableSplitPoint(open)).toBe(prefix.length);
    expect(findStableSplitPoint(closed)).toBe(closed.length);
  });

  it("keeps an unclosed tilde fence out of the stable prefix", () => {
    const prefix = `${"A".repeat(140)}\n\n`;
    const content = `${prefix}~~~ts\nconst first = 1;\n\nconst second = 2;`;

    expect(findStableSplitPoint(content)).toBe(prefix.length);
  });

  it("requires a closing fence to match the marker and opening length", () => {
    const prefix = `${"A".repeat(140)}\n\n`;
    const shortClose = `${prefix}\`\`\`\`ts\nconst value = 1;\n\`\`\`\n\nnot stable`;
    const wrongMarker = `${prefix}~~~~ts\nconst value = 1;\n\`\`\`\`\n\nnot stable`;
    const closed = `${prefix}\`\`\`\`ts\nconst value = 1;\n\`\`\`\``;

    expect(findStableSplitPoint(shortClose)).toBe(prefix.length);
    expect(findStableSplitPoint(wrongMarker)).toBe(prefix.length);
    expect(findStableSplitPoint(closed)).toBe(closed.length);
  });

  it("does not treat inline triple markers as a block fence", () => {
    const content = `${"A".repeat(140)} uses \`\`\`inline\`\`\` markers.\n\nNext paragraph`;
    const expected = content.indexOf("\n\n") + 2;

    expect(findStableSplitPoint(content)).toBe(expected);
  });

  it("renders markdown that begins after a long plain-text prefix", () => {
    const { container } = render(
      <MarkdownRenderer content={`${"A".repeat(501)}\n\n## Late heading`} />,
    );

    expect(container.querySelector("h2")?.textContent).toBe("Late heading");
  });

  it("renders code block text while syntax highlighting loads lazily", () => {
    render(<MarkdownRenderer content={"```ts\nconst value = 1;\n```"} />);

    expect(screen.getByText("ts")).toBeTruthy();
    expect(screen.getByText("复制")).toBeTruthy();
    expect(screen.getByText("const value = 1;")).toBeTruthy();
    expect(screen.getByText("1")).toBeTruthy();
  });

  it("renders Mermaid fenced code blocks as diagrams", async () => {
    const { container } = render(
      <MarkdownRenderer content={"```mermaid\ngraph TD\n  A-->B\n```"} />,
    );

    expect(screen.getByText("mermaid")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("md-mermaid")).toBeTruthy());
    expect(container.querySelector(".md-mermaid svg")).toBeTruthy();
    expect(document.body.textContent).toContain("Rendered Mermaid");
  });

  it("sanitizes Mermaid SVG before injecting it into the DOM", async () => {
    vi.mocked(mermaid.render).mockResolvedValueOnce({
      svg: [
        `<svg role="img">`,
        `<script>alert("x")</script>`,
        `<style>@import url("https://evil.example/x.css")</style>`,
        `<foreignObject><div>bad</div></foreignObject>`,
        `<a href="javascript:alert(1)"><text onclick="evil()">Safe Mermaid</text></a>`,
        `</svg>`,
      ].join(""),
    });

    const { container } = render(
      <MarkdownRenderer content={"```mermaid\ngraph TD\n  A-->B\n```"} />,
    );

    await waitFor(() => expect(screen.getByTestId("md-mermaid")).toBeTruthy());
    expect(container.querySelector(".md-mermaid svg")).toBeTruthy();
    expect(container.querySelector(".md-mermaid script")).toBeNull();
    expect(container.querySelector(".md-mermaid style")).toBeNull();
    expect(container.querySelector(".md-mermaid foreignObject")).toBeNull();
    expect(container.querySelector(".md-mermaid text")?.getAttribute("onclick")).toBeNull();
    expect(container.querySelector(".md-mermaid a")?.getAttribute("href")).toBeNull();
    expect(document.body.textContent).toContain("Safe Mermaid");
    expect(document.body.textContent).not.toContain("bad");
  });

  it("preserves safe Mermaid theme CSS so diagram nodes keep their colors", async () => {
    vi.mocked(mermaid.render).mockResolvedValueOnce({
      svg: [
        `<svg role="img">`,
        `<style>.node rect { fill: #eef2ff; stroke: #334155; } .nodeLabel { color: #111827; }</style>`,
        `<g class="node"><rect width="40" height="20"/><text class="nodeLabel">Safe</text></g>`,
        `</svg>`,
      ].join(""),
    });

    const { container } = render(
      <MarkdownRenderer content={"```mermaid\ngraph TD\n  A-->B\n```"} />,
    );

    await waitFor(() => expect(screen.getByTestId("md-mermaid")).toBeTruthy());
    expect(mermaid.initialize).toHaveBeenCalledWith(expect.objectContaining({
      flowchart: { htmlLabels: false },
    }));
    expect(container.querySelector(".md-mermaid style")?.textContent).toContain("fill: #eef2ff");
  });

  it("renders slash-separated prose options without an inline code pill", () => {
    const { container } = render(
      <MarkdownRenderer content={"可以按 `国内 / 国际 / 财经 / 科技` 展开。"} />,
    );

    expect(container.querySelector(".md-inline-option-list")).toBeTruthy();
    expect(container.querySelector("code")).toBeNull();
    expect(document.body.textContent).toContain("国内/国际/财经/科技");
  });

  it("renders ordinary prose inside backticks as prose instead of code", () => {
    const { container } = render(
      <MarkdownRenderer content={"我会核实 `豆包` 在 `2026-06-26 01:24 CST` 的价格。"} />,
    );

    expect(container.querySelector("code")).toBeNull();
    expect(container.querySelectorAll(".md-inline-code-prose")).toHaveLength(2);
    expect(screen.getByText(/豆包/)).toBeTruthy();
    expect(screen.getByText(/2026-06-26 01:24 CST/)).toBeTruthy();
  });

  it("keeps real inline code as code", () => {
    const { container } = render(<MarkdownRenderer content={"Use `const value = 1` here."} />);

    expect(container.querySelector("code")?.textContent).toBe("const value = 1");
    expect(container.querySelector(".md-inline-option-list")).toBeNull();
  });

  it("turns bare file references with line numbers into editor links", () => {
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const openEditorFile = vi.fn();
    useAppStore.setState({ openEditorFile });

    try {
      render(<MarkdownRenderer content={"Check frontend/src.v2/model/transformer.ts:42:7 before training."} />);

      fireEvent.click(screen.getByRole("button", { name: "frontend/src.v2/model/transformer.ts:42:7" }));

      expect(openEditorFile).toHaveBeenCalledWith(
        "frontend/src.v2/model/transformer.ts",
        undefined,
        { line: 42, column: 7 },
      );
      expect(screen.getByRole("button", { name: "frontend/src.v2/model/transformer.ts:42:7" }).className).toContain("md-file-chip");
    } finally {
      useAppStore.setState({ openEditorFile: originalOpenEditorFile });
    }
  });

  it("turns bare file references without line numbers into editor chips", () => {
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const openEditorFile = vi.fn();
    useAppStore.setState({ openEditorFile });

    try {
      render(
        <MarkdownRenderer
          content={[
            "几个关键文件：",
            "",
            "- backend/agent/loop.py：主循环。",
            "- backend/agent/loop_process_events.py：事件整理。",
          ].join("\n")}
        />,
      );

      const first = screen.getByRole("button", { name: "backend/agent/loop.py" });
      const second = screen.getByRole("button", { name: "backend/agent/loop_process_events.py" });
      expect(first.className).toContain("md-file-chip");
      expect(first.className).toContain("no-underline");
      expect(second.className).toContain("md-file-chip");

      fireEvent.click(first);

      expect(openEditorFile).toHaveBeenCalledWith(
        "backend/agent/loop.py",
        undefined,
        { line: undefined, column: undefined },
      );
    } finally {
      useAppStore.setState({ openEditorFile: originalOpenEditorFile });
    }
  });

  it("renders inline-code file references as editor chips", () => {
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const openEditorFile = vi.fn();
    useAppStore.setState({ openEditorFile });

    try {
      render(<MarkdownRenderer content={"- `backend/main.py`, `backend/bootstrap/app.py`：启动入口。"} />);

      const chip = screen.getByRole("button", { name: "backend/main.py" });
      expect(chip.className).toContain("md-file-chip");
      expect(document.querySelector("code")).toBeNull();

      fireEvent.click(chip);

      expect(openEditorFile).toHaveBeenCalledWith(
        "backend/main.py",
        undefined,
        { line: undefined, column: undefined },
      );
    } finally {
      useAppStore.setState({ openEditorFile: originalOpenEditorFile });
    }
  });

  it("renders editor chips with compact file-type badges", () => {
    render(<MarkdownRenderer content={"Open `frontend/src.v2/chat/noticeEvents.ts` and `README.md`."} />);

    const tsChip = screen.getByRole("button", { name: "frontend/src.v2/chat/noticeEvents.ts" });
    const mdChip = screen.getByRole("button", { name: "README.md" });
    expect(tsChip.getAttribute("data-ext")).toBe("ts");
    expect(mdChip.getAttribute("data-ext")).toBe("md");
    expect(tsChip.querySelector('.md-official-file-icon[data-document-type="ts"]')).toBeTruthy();
    expect(mdChip.querySelector('.md-official-file-icon[data-document-type="md"]')).toBeTruthy();
    expect(tsChip.querySelector(".md-file-chip-name")?.textContent).toBe("noticeEvents.ts");
  });

  it("renders file line labels as muted chip metadata", () => {
    render(<MarkdownRenderer content={"[noticeEvents.ts (line 35)](frontend/src.v2/chat/noticeEvents.ts)"} />);

    const chip = screen.getByRole("button", { name: "noticeEvents.ts (line 35)" });
    expect(chip.querySelector(".md-file-chip-name")?.textContent).toBe("noticeEvents.ts");
    expect(chip.querySelector(".md-file-chip-meta")?.textContent).toBe("(line 35)");
  });

  it("renders localhost workspace file markdown links as editor chips", () => {
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    const openEditorFile = vi.fn();
    useAppStore.setState({
      openEditorFile,
      workingDirectory: "C:/Desktop/MiniCode",
    });

    try {
      render(
        <MarkdownRenderer content={"- [backend/agent/loop.py](http://127.0.0.1:5173/C:/Desktop/MiniCode/backend/agent/loop.py)：主循环。"} />,
      );

      const chip = screen.getByRole("button", { name: "backend/agent/loop.py" });
      expect(chip.className).toContain("md-file-chip");
      expect(chip.className).toContain("no-underline");
      expect(chip.getAttribute("title")).toBe("在编辑器中打开 C:/Desktop/MiniCode/backend/agent/loop.py");
      expect(document.querySelector('a[href*="backend/agent/loop.py"]')).toBeNull();

      fireEvent.click(chip);

      expect(openEditorFile).toHaveBeenCalledWith(
        "C:/Desktop/MiniCode/backend/agent/loop.py",
        undefined,
        { line: undefined, column: undefined },
      );
    } finally {
      useAppStore.setState({
        openEditorFile: originalOpenEditorFile,
        workingDirectory: originalWorkingDirectory,
      });
    }
  });

  it("renders root-relative localhost file links as workspace editor chips", () => {
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    const openEditorFile = vi.fn();
    useAppStore.setState({
      openEditorFile,
      workingDirectory: "C:/Desktop/MiniCode",
    });

    try {
      render(
        <MarkdownRenderer content={"[backend/agent/loop.py](http://127.0.0.1:5173/backend/agent/loop.py), 继续。"} />,
      );

      const chip = screen.getByRole("button", { name: "backend/agent/loop.py" });
      expect(chip.className).toContain("md-file-chip");
      expect(document.querySelector('a[href*="backend/agent/loop.py"]')).toBeNull();

      fireEvent.click(chip);

      expect(openEditorFile).toHaveBeenCalledWith(
        "backend/agent/loop.py",
        undefined,
        { line: undefined, column: undefined },
      );
    } finally {
      useAppStore.setState({
        openEditorFile: originalOpenEditorFile,
        workingDirectory: originalWorkingDirectory,
      });
    }
  });

  it("renders localhost workspace folder links as revealable folder chips", () => {
    const originalRequestFileTreeReveal = useAppStore.getState().requestFileTreeReveal;
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    const requestFileTreeReveal = vi.fn();
    useAppStore.setState({
      requestFileTreeReveal,
      workingDirectory: "C:/Desktop/MiniCode",
    });

    try {
      render(
        <MarkdownRenderer content={"- [backend](http://127.0.0.1:5173/C:/Desktop/MiniCode/backend)：后端。"} />,
      );

      const chip = screen.getByRole("button", { name: "backend" });
      expect(chip.className).toContain("md-folder-chip");
      expect(chip.querySelector(".md-folder-chip-icon")).toBeTruthy();

      fireEvent.click(chip);

      expect(requestFileTreeReveal).toHaveBeenCalledWith("C:/Desktop/MiniCode/backend", "folder");
      expect(document.querySelector('a[href*="backend"]')).toBeNull();
    } finally {
      useAppStore.setState({
        requestFileTreeReveal: originalRequestFileTreeReveal,
        workingDirectory: originalWorkingDirectory,
      });
    }
  });

  it("renders generated PDF links as files instead of folders", () => {
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });

    try {
      render(
        <MarkdownRenderer content={"[report.pdf](C:\\Desktop\\MiniCode\\outputs\\report.pdf)"} />,
      );

      const chip = screen.getByRole("button", { name: "report.pdf" });
      expect(chip.getAttribute("data-ext")).toBe("pdf");
      expect(chip.getAttribute("data-kind")).not.toBe("folder");
      expect(chip.querySelector('.md-official-file-icon[data-document-type="pdf"]')).toBeTruthy();

      fireEvent.click(chip);
      expect(openPathMock).not.toHaveBeenCalled();
      expect(useAppStore.getState().previewArtifact).toMatchObject({
        name: "report.pdf",
        source: "workspace",
        loading: true,
      });

      fireEvent.contextMenu(chip, { clientX: 20, clientY: 20 });
      fireEvent.click(screen.getByText("在资源管理器中显示"));
      expect(revealPathMock).toHaveBeenCalledWith("C:/Desktop/MiniCode/outputs/report.pdf");
    } finally {
      useAppStore.setState({ workingDirectory: originalWorkingDirectory });
    }
  });

  it("preserves the full Windows path for generated file links", () => {
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });
    render(
      <MarkdownRenderer content={"[WorldCup2026_Semifinal.pdf](C:\\Desktop\\MiniCode\\desktop\\release\\win-unpacked\\resources\\WorldCup2026_Semifinal.pdf)"} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "WorldCup2026_Semifinal.pdf" }));
    expect(openPathMock).not.toHaveBeenCalled();
    expect(useAppStore.getState().previewArtifact).toMatchObject({
      name: "WorldCup2026_Semifinal.pdf",
      source: "workspace",
    });
  });

  it("opens a generated document outside the workspace with the default app", () => {
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });
    render(<MarkdownRenderer content={"[test.docx](C:\\Desktop\\test.docx)"} />);

    const chip = screen.getByRole("button", { name: "test.docx" });
    expect(chip.getAttribute("data-ext")).toBe("docx");
    fireEvent.click(chip);
    expect(openPathMock).toHaveBeenCalledWith("C:/Desktop/test.docx");

    fireEvent.contextMenu(chip, { clientX: 20, clientY: 20 });
    fireEvent.click(screen.getByText("在资源管理器中显示"));
    expect(revealPathMock).toHaveBeenCalledWith("C:/Desktop/test.docx");
  });

  it("turns an inline-code external document path into an openable file chip", () => {
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });
    render(<MarkdownRenderer content={"文档位于 `C:\\Desktop\\test.docx`。"} />);

    const chip = screen.getByRole("button", { name: "C:\\Desktop\\test.docx" });
    fireEvent.click(chip);
    expect(openPathMock).toHaveBeenCalledWith("C:/Desktop/test.docx");
  });

  it("opens persisted read-only tool-result txt links through the desktop path gate", () => {
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });

    try {
      const path = "C:/Users/ago/AppData/Roaming/minicode-desktop/data/tool-results/mc_web_fetch_example.txt";
      const href = `minicode-local-file:${encodeURIComponent(path)}`;
      render(<MarkdownRenderer content={`Full output path: [${path}](${href})`} />);

      const chip = screen.getByRole("button", { name: path });
      expect(chip.getAttribute("data-ext")).toBe("txt");
      fireEvent.click(chip);
      expect(openPathMock).toHaveBeenCalledWith(path);
    } finally {
      useAppStore.setState({ workingDirectory: originalWorkingDirectory });
    }
  });

  it("renders the persisted cleanup PDF link as a file chip", () => {
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode/desktop/release/win-unpacked/resources" });
    render(
      <MarkdownRenderer content={"已清理 `worldcup_report.py`。PDF 报告 [WorldCup2026_Semifinal.pdf](C:\\Desktop\\MiniCode\\desktop\\release\\win-unpacked\\resources\\WorldCup2026_Semifinal.pdf) 保留。"} />,
    );

    expect(screen.getByRole("button", { name: "WorldCup2026_Semifinal.pdf" })).toBeTruthy();
  });

  it("does not hijack external web links just because the label looks like a file", () => {
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const openEditorFile = vi.fn();
    useAppStore.setState({ openEditorFile });

    try {
      render(<MarkdownRenderer content={"[backend/agent/loop.py](https://example.com/backend/agent/loop.py)"} />);

      expect(screen.queryByRole("button", { name: "backend/agent/loop.py" })).toBeNull();
      expect(screen.getByRole("link", { name: "backend/agent/loop.py" })).toBeTruthy();
      expect(openEditorFile).not.toHaveBeenCalled();
    } finally {
      useAppStore.setState({ openEditorFile: originalOpenEditorFile });
    }
  });

  it("does not hijack external web links just because the label looks like a folder", () => {
    const requestFileTreeReveal = vi.fn();
    const originalRequestFileTreeReveal = useAppStore.getState().requestFileTreeReveal;
    useAppStore.setState({ requestFileTreeReveal });

    try {
      render(<MarkdownRenderer content={"[backend](https://example.com/backend)"} />);

      expect(screen.queryByRole("button", { name: "backend" })).toBeNull();
      expect(screen.getByRole("link", { name: "backend" })).toBeTruthy();
      expect(requestFileTreeReveal).not.toHaveBeenCalled();
    } finally {
      useAppStore.setState({ requestFileTreeReveal: originalRequestFileTreeReveal });
    }
  });

  it("opens ordinary web links inside the Browser panel", () => {
    useAppStore.setState({ conversationId: "conv-markdown-link" });
    const requests: string[] = [];
    const unsubscribe = subscribeBrowserOpenRequests((request) => requests.push(request.url));
    render(<MarkdownRenderer content={"Open [docs](https://docs.example/guide)."} />);

    const link = screen.getByRole("link", { name: "docs" });
    expect(link.getAttribute("target")).toBeNull();
    fireEvent.click(link);

    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(requests).toEqual(["https://docs.example/guide"]);
    expect(sendMock).not.toHaveBeenCalledWith({ type: "preview.navigate", url: "https://docs.example/guide" });
    unsubscribe();
  });

  it("renders descriptive external links with a website icon and keeps their label", () => {
    const { container } = render(
      <MarkdownRenderer
        content={"[NeurIPS 2026 官方时间表](https://neurips.cc/Conferences/2026/Dates)"}
      />,
    );

    const link = screen.getByRole("link", { name: "NeurIPS 2026 官方时间表" });
    expect(link.getAttribute("href")).toBe("https://neurips.cc/Conferences/2026/Dates");
    expect(link.classList.contains("md-web-link")).toBe(true);
    expect(container.querySelector(".md-web-link .brand-icon")).toBeTruthy();
  });

  it("deduplicates rapid repeated Browser navigation for the same url", () => {
    useAppStore.setState({ conversationId: "conv-markdown-dedupe" });
    const requests: string[] = [];
    const unsubscribe = subscribeBrowserOpenRequests((request) => requests.push(request.url));
    render(<MarkdownRenderer content={"Open [docs](https://docs.example/guide)."} />);

    const link = screen.getByRole("link", { name: "docs" });
    fireEvent.click(link);
    fireEvent.click(link);

    expect(requests).toEqual(["https://docs.example/guide"]);
    expect(useAppStore.getState().rightStackTab).toBe("browser");
    unsubscribe();
  });

  it("opens http images inside the Browser panel instead of a new browser window", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    useAppStore.setState({ conversationId: "conv-markdown-image" });
    const requests: string[] = [];
    const unsubscribe = subscribeBrowserOpenRequests((request) => requests.push(request.url));

    try {
      render(<MarkdownRenderer content={"![chart](https://assets.example/chart.png)"} />);

      expect(screen.queryByRole("img", { name: "chart" })).toBeNull();
      expect(screen.getByText(/图片来自 assets\.example/)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "加载图片" }));
      fireEvent.click(screen.getByRole("img", { name: "chart" }));

      expect(openSpy).not.toHaveBeenCalled();
      expect(useAppStore.getState().rightStackTab).toBe("browser");
      expect(requests).toEqual(["https://assets.example/chart.png"]);
    } finally {
      unsubscribe();
      openSpy.mockRestore();
    }
  });

  it("requires separate permission when a streamed remote image URL changes", () => {
    const { rerender } = render(<MarkdownRenderer content={"![chart](https://assets.example/a.png)"} />);

    fireEvent.click(screen.getByRole("button", { name: "加载图片" }));
    expect(screen.getByRole("img", { name: "chart" }).getAttribute("src")).toBe("https://assets.example/a.png");

    rerender(<MarkdownRenderer content={"![chart](https://cdn.example/b.png)"} />);

    expect(screen.queryByRole("img", { name: "chart" })).toBeNull();
    expect(screen.getByText(/图片来自 cdn\.example/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "加载图片" })).toBeTruthy();
  });

  it("allows an exact remote image domain for the current task", () => {
    render(<MarkdownRenderer content={"![one](https://assets.example/a.png)\n\n![two](https://assets.example/b.png)"} />);

    fireEvent.click(screen.getAllByRole("button", { name: "本任务允许 assets.example" })[0]);

    expect(screen.getByRole("img", { name: "one" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "two" })).toBeTruthy();
    expect(useAppStore.getState().allowedRemoteImageDomains).toEqual(["assets.example"]);
  });

  it("honors global allow and block policies", () => {
    useAppStore.setState({ remoteImagePolicy: "allow" });
    const { rerender } = render(<MarkdownRenderer content={"![chart](https://assets.example/a.png)"} />);
    expect(screen.getByRole("img", { name: "chart" })).toBeTruthy();

    useAppStore.setState({ remoteImagePolicy: "block" });
    rerender(<MarkdownRenderer content={"![chart](https://assets.example/a.png)"} />);
    expect(screen.queryByRole("img", { name: "chart" })).toBeNull();
    expect(screen.getByText("设置中已禁止加载远程图片。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "加载图片" })).toBeNull();
  });

  it("opens data images in the shared file preview", () => {
    render(<MarkdownRenderer content={"![inline](data:image/png;base64,AAAA)"} />);

    fireEvent.click(screen.getByRole("img", { name: "inline" }));

    expect(useAppStore.getState().previewArtifact).toMatchObject({
      name: "inline",
      source: "local",
      mediaType: "image/png",
      url: "data:image/png;base64,AAAA",
    });
    expect(sendMock).not.toHaveBeenCalled();
  });

  it("does not allow SVG data image URLs", () => {
    render(<MarkdownRenderer content={"![svg](data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+)"} />);

    const img = screen.getByRole("img", { name: "svg" });
    expect(img.getAttribute("src") || "").not.toContain("data:image/svg+xml");
  });

  it("does not open non-http images in an external browser", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    try {
      render(<MarkdownRenderer content={"![inline](data:image/png;base64,AAAA)"} />);

      fireEvent.click(screen.getByRole("img", { name: "inline" }));

      expect(openSpy).not.toHaveBeenCalled();
      expect(sendMock).not.toHaveBeenCalled();
    } finally {
      openSpy.mockRestore();
    }
  });

  it("serves workspace Windows absolute image paths through the signed raw endpoint", () => {
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });

    try {
      render(<MarkdownRenderer content={String.raw`![local](C:\Desktop\MiniCode\tmp\shot.png)`} />);

      const img = screen.getByRole("img", { name: "local" });
      const src = img.getAttribute("src") || "";
      expect(src).toContain("/api/workspace/raw");
      expect(src).toContain("path=tmp%2Fshot.png");
    } finally {
      useAppStore.setState({ workingDirectory: originalWorkingDirectory });
    }
  });

  it("serves POSIX absolute image paths only when they are inside the active workspace", () => {
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    useAppStore.setState({ workingDirectory: "/tmp/Project" });

    try {
      render(<MarkdownRenderer content={"![local](/tmp/Project/assets/shot.png)"} />);

      const img = screen.getByRole("img", { name: "local" });
      const src = img.getAttribute("src") || "";
      expect(src).toContain("/api/workspace/raw");
      expect(src).toContain("path=assets%2Fshot.png");
    } finally {
      useAppStore.setState({ workingDirectory: originalWorkingDirectory });
    }
  });

  it("blocks a differently-cased POSIX absolute image path", () => {
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    useAppStore.setState({ workingDirectory: "/tmp/Project" });

    try {
      render(<MarkdownRenderer content={"![local](/tmp/project/assets/shot.png)"} />);

      const placeholder = screen.getByRole("img", { name: "local" });
      expect(placeholder.tagName.toLowerCase()).toBe("span");
      expect(placeholder.getAttribute("src")).toBeNull();
      expect(document.querySelector("img")).toBeNull();
    } finally {
      useAppStore.setState({ workingDirectory: originalWorkingDirectory });
    }
  });

  it("blocks local image paths outside the workspace", () => {
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });

    try {
      render(<MarkdownRenderer content={String.raw`![local](C:\Users\ago\AppData\Local\Temp\shot.png)`} />);

      const placeholder = screen.getByRole("img", { name: "local" });
      expect(placeholder.tagName.toLowerCase()).toBe("span");
      expect(placeholder.getAttribute("src")).toBeNull();
      expect(document.querySelector("img")).toBeNull();
    } finally {
      useAppStore.setState({ workingDirectory: originalWorkingDirectory });
    }
  });

  it("does not turn absolute or traversal file references into editor links", () => {
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const openEditorFile = vi.fn();
    useAppStore.setState({ openEditorFile });

    try {
      render(<MarkdownRenderer content={String.raw`Check C:\Users\ago\secret.ts:1 and ../secret.ts:2.`} />);

      expect(screen.queryByRole("button", { name: String.raw`C:\Users\ago\secret.ts:1` })).toBeNull();
      expect(screen.queryByRole("button", { name: "../secret.ts:2" })).toBeNull();
      expect(openEditorFile).not.toHaveBeenCalled();
    } finally {
      useAppStore.setState({ openEditorFile: originalOpenEditorFile });
    }
  });

  it("renders unordered lists with disc markers", () => {
    const { container } = render(<MarkdownRenderer content={"- one\n- two"} />);

    const ul = container.querySelector("ul");
    expect(ul).toBeTruthy();
    expect(ul?.className).toContain("list-disc");
    expect(screen.getByText("one")).toBeTruthy();
    expect(screen.getByText("two")).toBeTruthy();
  });

  it("does not leak react-markdown node metadata into DOM attributes", () => {
    const { container } = render(<MarkdownRenderer content={"## Heading\n\nParagraph"} />);

    expect(container.querySelector("h2")?.hasAttribute("node")).toBe(false);
    expect(container.querySelector("p")?.hasAttribute("node")).toBe(false);
  });

  it("renders ordered lists with decimal markers", () => {
    const { container } = render(<MarkdownRenderer content={"1. first\n2. second"} />);

    const ol = container.querySelector("ol");
    expect(ol).toBeTruthy();
    expect(ol?.className).toContain("list-decimal");
    expect(screen.getByText("first")).toBeTruthy();
  });

  it("renders GFM tables with wrapped horizontal scrolling", () => {
    const { container } = render(
      <MarkdownRenderer content={"| Name | Value |\n| --- | ---: |\n| alpha | 42 |"} />,
    );

    expect(container.querySelector("table")).toBeTruthy();
    expect(container.querySelector("th")?.textContent).toBe("Name");
    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
  });

  it("renders streaming tail markdown instead of exposing raw markers", () => {
    const stablePrefix = `${"A".repeat(220)}\n\n`;
    render(<MarkdownRenderer content={`${stablePrefix}**tail bold**\n\n## Tail Heading`} isStreaming />);

    expect(document.body.textContent).not.toContain("**tail bold**");
    expect(screen.getByText("tail bold").tagName.toLowerCase()).toBe("strong");
    expect(screen.getByRole("heading", { name: "Tail Heading", level: 2 })).toBeTruthy();
  });

  it("renders bracket-delimited LaTeX as display math", () => {
    const { container } = render(
      <MarkdownRenderer content={String.raw`已知 \[ a_1=3,\qquad a_{n+1}=\frac{a_n}{n}+\frac1{n(n+1)}. \]`} />,
    );

    expect(container.querySelector(".katex-display")).toBeTruthy();
    expect(container.querySelector(".katex")).toBeTruthy();
    expect(document.body.textContent).not.toContain(String.raw`\[`);
    expect(document.body.textContent).not.toContain("[ a_1=3");
  });

  it("renders parenthesized LaTeX as inline math", () => {
    const { container } = render(
      <MarkdownRenderer content={String.raw`通项为 \(a_n=1+\frac2n\)，所以不是等差数列。`} />,
    );

    expect(container.querySelector(".katex")).toBeTruthy();
    expect(container.querySelector(".katex-display")).toBeNull();
    expect(document.body.textContent).not.toContain(String.raw`\(`);
    expect(document.body.textContent).not.toContain("(a_n=1");
  });

  it("normalizes model display-math blocks that close after prose", () => {
    const content = [
      "搞定，精确值是",
      "",
      "$$\\boxed{I=\\frac{7}{2}\\,\\zeta(3)\\ln 2-\\frac{19}{8}\\zeta(4)}$$",
      "",
      "**2. 先对 $k$ 求和。**",
      "$$\\frac{I}{2}=\\zeta(3)\\ln2-C",
      "=\\zeta(3)\\ln2-C .$$",
      "",
      "**3. 线性交错 Euler 和 $C$ 的标准值。**",
      "$$\\sum_{n\\ge1}\\frac{(-1)^{n-1}H_n^{(3)}}{n}=C=\\frac{19}{16}\\zeta(4)-\\frac{3}{4}\\zeta(3)\\ln2 .$$",
    ].join("\n");

    const { container } = render(<MarkdownRenderer content={content} />);

    expect(container.querySelectorAll(".katex-error")).toHaveLength(0);
    expect(container.querySelectorAll(".katex-display").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText(/2\. 先对/)).toBeTruthy();
    expect(document.body.textContent).toContain("线性交错 Euler 和");
    expect(document.body.textContent).not.toContain("**3. 线性交错 Euler");
  });

  it("renders GFM task lists with checkboxes and preserves marker-suppression classes", () => {
    const { container } = render(<MarkdownRenderer content={"- [ ] todo\n- [x] done"} />);

    // Checkbox replaces the bullet; both items render an input
    const checkboxes = container.querySelectorAll('input[type="checkbox"]');
    expect(checkboxes.length).toBe(2);
    // The GFM class names must survive className merging so the CSS can strip markers
    expect(container.querySelector("ul.contains-task-list")).toBeTruthy();
    expect(container.querySelector("li.task-list-item")).toBeTruthy();
  });

  it("does not render Chinese range text with single tildes as strikethrough", () => {
    const { container } = render(
      <MarkdownRenderer content={"北转南风 2~3级，雷雨时短时阵风 6~7级。"} />,
    );

    expect(screen.getByText("北转南风 2~3级，雷雨时短时阵风 6~7级。")).toBeTruthy();
    expect(container.querySelector("del")).toBeNull();
  });

  it("preserves model-authored source lines with bare domains", () => {
    const { container } = render(
      <MarkdownRenderer content={"北京今天阴。\n\n数据来源：中央气象台 nmc.cn"} />,
    );

    expect(screen.getByText("北京今天阴。")).toBeTruthy();
    expect(document.body.textContent).toContain("数据来源：中央气象台 nmc.cn");
    expect(container.querySelector('a[href*="nmc.cn"]')).toBeNull();
  });

  it("preserves source footers, standalone markdown links, and model markers", () => {
    const { container } = render(
      <MarkdownRenderer
        content={[
          "北京今天晴，气温舒适。",
          "",
          "来源：中国天气网",
          "",
          "[weather.com.cn](https://www.weather.com.cn/weather/101010100.shtml)",
          "、中央气象台 [2]",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("北京今天晴，气温舒适。")).toBeTruthy();
    expect(document.body.textContent).toContain("来源：中国天气网");
    expect(document.body.textContent).toContain("weather.com.cn");
    expect(document.body.textContent).toContain("[2]");
    expect(container.querySelectorAll("a")).toHaveLength(1);
    expect(container.querySelector(".md-web-link .brand-icon")).toBeTruthy();
  });

  it("preserves source text even when a domain suffix is split by whitespace", () => {
    render(
      <MarkdownRenderer
        content={[
          "北京今天晴，风力不大。",
          "",
          "来源：中国天气网 weather.com.c n",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("北京今天晴，风力不大。")).toBeTruthy();
    expect(document.body.textContent).toContain("来源：中国天气网 weather.com.c n");
  });

  it("resolves workspace-relative image paths from the workspace root", () => {
    const originalWorkingDirectory = useAppStore.getState().workingDirectory;
    useAppStore.setState({ workingDirectory: "C:/Desktop/MiniCode" });

    try {
      render(<MarkdownRenderer content={"![local](docs/images/diagram.png)"} />);

      const img = screen.getByRole("img", { name: "local" });
      const src = img.getAttribute("src") || "";
      expect(src).toContain("/api/workspace/raw");
      expect(src).toContain("path=docs%2Fimages%2Fdiagram.png");
      expect(screen.queryByRole("button", { name: "加载图片" })).toBeNull();
    } finally {
      useAppStore.setState({ workingDirectory: originalWorkingDirectory });
    }
  });

  it("wraps previewable images in a semantic activation button", () => {
    render(<MarkdownRenderer content={"![inline](data:image/png;base64,AAAA)"} />);

    fireEvent.click(screen.getByRole("button", { name: "在预览中打开 inline" }));

    expect(useAppStore.getState().previewArtifact).toMatchObject({ source: "local", name: "inline" });
  });

  it("preserves bold source footers without treating a country-code domain as a C file", () => {
    const { container } = render(
      <MarkdownRenderer
        content={[
          "北京今天小雨转多云。",
          "",
          "**数据来源：** 中国天气网（weather.com.cn），数据更新时间 2026-07-12 18:00。",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("北京今天小雨转多云。")).toBeTruthy();
    expect(document.body.textContent).toContain("数据来源：");
    expect(document.body.textContent).toContain("weather.com.cn");
    expect(container.querySelector(".md-file-chip")).toBeNull();
  });

  it("preserves compact indexed source footers with titles and URLs", () => {
    const { container } = render(
      <MarkdownRenderer
        content={[
          "今天的主要新闻如下：",
          "",
          "[1] 新浪国际热点小时报 https://news.example/a [2] SBS中文 https://sbs.example/b [3] 新浪英超热点小时报 https://sports.example/c",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("今天的主要新闻如下：")).toBeTruthy();
    expect(document.body.textContent).toContain("新浪国际热点小时报");
    expect(document.body.textContent).toContain("SBS中文");
    expect(document.body.textContent).toContain("news.example");
    expect(container.querySelectorAll("a").length).toBeGreaterThanOrEqual(3);
  });

  it("preserves mixed markdown source footers with escaped citation labels", () => {
    const { container } = render(
      <MarkdownRenderer
        content={[
          "今天的主要新闻如下：",
          "",
          String.raw`[[1\]](https://cj.sina.cn/articles/view/7857201856/1d45362c0019074efw?froms=ggmp) 新浪国际热点小时报 [https://cj.sina.cn/articles/view/7857201856/1d45362c0019074efw](https://cj.sina.cn/articles/view/7857201856/1d45362c0019074efw) [[2\]](https://sports.sina.cn/2026-06-21/detail-iniecryz8006849.d.html?vt=4&cid=72264&node_id=72264) SBS中文 [https://www.sbs.com.au/language/chinese/zh-hans/article/sbs-news-in-chinese/7gz51ti8v](https://www.sbs.com.au/language/chinese/zh-hans/article/sbs-news-in-chinese/7gz51ti8v) [[3\]](https://www.usvigers.com/calendar-of-events/tag/countdown/day/2026-06-21) 新浪英超热点小时报 [https://sports.sina.cn/2026-06-21/detail-iniecryz8006849.d.html](https://sports.sina.cn/2026-06-21/detail-iniecryz8006849.d.html)`,
        ].join("\n")}
      />,
    );

    expect(screen.getByText("今天的主要新闻如下：")).toBeTruthy();
    expect(document.body.textContent).toContain("新浪国际热点小时报");
    expect(document.body.textContent).toContain("SBS中文");
    expect(document.body.textContent).toContain("sina.cn");
    expect(container.querySelectorAll("a").length).toBeGreaterThan(0);
  });

  it("preserves inline domain-only source links and answer prose", () => {
    const { container } = render(
      <MarkdownRenderer
        content={"北半球迎来全年最长白昼[democracynow.org](https://www.democracynow.org/2026/6/19/headlines)。"}
      />,
    );

    expect(document.body.textContent).toContain("北半球迎来全年最长白昼");
    expect(document.body.textContent).toContain("democracynow.org");
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });

  it("keeps source-like markdown links inside fenced code blocks", () => {
    render(
      <MarkdownRenderer
        content={"```md\n[example.com](https://example.com)\n```"}
      />,
    );

    expect(document.body.textContent).toContain("[example.com](https://example.com)");
  });

  it("does not silently delete truncation-looking model text", () => {
    render(
      <MarkdownRenderer
        content={[
          "已检查 `frontend/src.v2/chat/messages/MarkdownRenderer.tsx`。",
          "",
          "...[内容截断]...",
          "",
          "继续看 `frontend/src.v2/chat/messages/citationText.ts`。",
        ].join("\n")}
      />,
    );

    expect(document.body.textContent).toContain("已检查");
    expect(document.body.textContent).toContain("继续看");
    expect(document.body.textContent).toContain("内容截断");
  });

  it("keeps truncation-looking text inside fenced code blocks", () => {
    render(
      <MarkdownRenderer
        content={"```txt\n...[内容截断]...\n[truncated 120 chars]\n```"}
      />,
    );

    expect(document.body.textContent).toContain("...[内容截断]...");
    expect(document.body.textContent).toContain("[truncated 120 chars]");
  });

  it("preserves model-authored citation summary footers without URLs", () => {
    render(
      <MarkdownRenderer
        content={[
          "谈判预计于6月21日上午正式开始。",
          "",
          "来源：[1] 证券时报；[2] 新京报；[3] 纽约时报中文网。信息截至北京时间2026年6月21日21:50。",
        ].join("\n")}
      />,
    );

    expect(screen.getByText("谈判预计于6月21日上午正式开始。")).toBeTruthy();
    expect(document.body.textContent).toContain("来源：[1] 证券时报；[2] 新京报；[3] 纽约时报中文网");
    expect(document.body.textContent).toContain("信息截至");
  });

  it("keeps model-authored inline citation markers without structured citations", () => {
    render(
      <MarkdownRenderer
        content={[
          "第一条新闻来自官方通报 [1]，第二条来自现场发布 [2]。",
          "",
          "来源：[1] 证券时报；[2] 新京报。",
        ].join("\n")}
      />,
    );

    expect(document.body.textContent).toContain("第一条新闻来自官方通报 [1]，第二条来自现场发布 [2]。");
    expect(document.body.textContent).toContain("来源：[1] 证券时报；[2] 新京报。");
  });

  it("keeps bracketed numbers when there is no model-authored source footer", () => {
    render(<MarkdownRenderer content={"数组 arr[1] 是第二个元素，备注 [12] 保留。"} />);

    expect(screen.getByText("数组 arr[1] 是第二个元素，备注 [12] 保留。")).toBeTruthy();
  });

  it("removes structured inline citation markers from answer prose", () => {
    render(
      <MarkdownRenderer
        content={"北京天气参考 [1]，备用来源 [2]。"}
        citations={[{
          source: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
          url: "https://www.nmc.cn/publish/forecast/ABJ/beijing.html",
          label: "中央气象台",
          range: [0, 0],
        }]}
      />,
    );

    expect(screen.getByText(/北京天气参考/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "[1]" })).toBeNull();
    expect(document.body.textContent).not.toContain("[1]");
    expect(document.body.textContent).not.toContain("[2]");
    expect(screen.queryByRole("link", { name: "中央气象台" })).toBeNull();
    expect(document.querySelector(".assistant-inline-source-chip")).toBeNull();
  });
});
