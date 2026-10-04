// @vitest-environment jsdom
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MarkdownRenderer } from "./MarkdownRenderer";

const writeClipboard = vi.fn().mockResolvedValue(undefined);
const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
beforeEach(() => {
  writeClipboard.mockClear();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: writeClipboard } });
});
afterEach(() => {
  cleanup();
  if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
  else Reflect.deleteProperty(navigator, "clipboard");
});

describe.each([false, true])("Markdown semantics, streaming=%s", (isStreaming) => {
  it("removes only citation markers bound to actual source metadata", () => {
    const view = render(<MarkdownRenderer content="Known [1]. Unbound [2]." isStreaming={isStreaming}
      citations={[{ source: "https://example.test/source", range: [0, 0] }, { source: "", range: [0, 0] }]} />);
    expect(view.container.textContent).not.toContain("[1]");
    expect(view.container.textContent).toContain("[2]");
  });
  it.each([
    { content: "https://example.com", href: "https://example.com" },
    { content: "www.example.com", href: "http://www.example.com" },
    { content: "reader@example.com", href: "mailto:reader@example.com" },
  ])("autolinks $content without needing other Markdown markers", ({ content, href }) => {
    const view = render(<MarkdownRenderer content={content} isStreaming={isStreaming} />);
    expect(view.getByRole("link", { name: content }).getAttribute("href")).toBe(href);
  });

  it("decodes entities and explicit escapes in otherwise plain text", () => {
    const view = render(<MarkdownRenderer content={String.raw`AT&amp;T &#x41; \! \*literal\*`} isStreaming={isStreaming} />);
    expect(view.container.textContent).toBe("AT&T A ! *literal*");
    expect(view.container.querySelector("em")).toBeNull();
  });

  it("preserves explicitly escaped bold markers", () => {
    const view = render(<MarkdownRenderer content={String.raw`\*\*literal\*\* &ast;&ast;entity&ast;&ast;`} isStreaming={isStreaming} />);
    expect(view.container.textContent).toBe("**literal** **entity**");
    expect(view.container.querySelector("strong")).toBeNull();
  });

  it("preserves mixed escaped and entity markers alongside Chinese fallback", () => {
    const content = String.raw`AT&amp;T \! \*\*literal\*\* &ast;&ast;entity&ast;&ast; 前**加粗。**后`;
    const view = render(<MarkdownRenderer content={content} isStreaming={isStreaming} />);
    expect(view.container.textContent).toBe("AT&T ! **literal** **entity** 前加粗。后");
    expect([...view.container.querySelectorAll("strong")].map((node) => node.textContent)).toEqual(["加粗。"]);
  });

  it.each([
    { name: "even opening backslashes", content: String.raw`前\\**加粗。**后`, text: String.raw`前\加粗。后`, strong: ["加粗。"] },
    { name: "escaped closing marker", content: String.raw`前**加粗。\**后`, text: "前**加粗。**后", strong: [] },
    { name: "even closing backslashes", content: String.raw`前**加粗。\\**后`, text: String.raw`前加粗。\后`, strong: ["加粗。\\"] },
    { name: "entity and CRLF prefix", content: "AT&amp;T \\!\r\n前**加粗。**后 &ast;&ast;entity&ast;&ast;", text: "AT&T !\r\n前加粗。后 **entity**", strong: ["加粗。"] },
  ])("preserves fallback boundary semantics for $name", ({ content, text, strong }) => {
    const view = render(<MarkdownRenderer content={content} isStreaming={isStreaming} />);
    expect(view.container.textContent).toBe(text);
    expect([...view.container.querySelectorAll("strong")].map((node) => node.textContent)).toEqual(strong);
  });

  it("renders a hard line break in a single paragraph", () => {
    const view = render(<MarkdownRenderer content={"first  \nsecond"} isStreaming={isStreaming} />);
    expect(view.container.querySelector("p br")).not.toBeNull();
  });

  it.each(["+ item", "1) item"])("parses a one-line list: %s", (content) => {
    const view = render(<MarkdownRenderer content={content} isStreaming={isStreaming} />);
    expect(view.getByRole("listitem").textContent).toBe("item");
  });

  it("renders simple inline math with no other syntax", () => {
    const view = render(<MarkdownRenderer content="$x+y$" isStreaming={isStreaming} />);
    expect(view.container.querySelector(".katex")).not.toBeNull();
    expect(view.container.querySelector(".katex-error")).toBeNull();
  });

  it.each([1, 2, 3, 4, 5, 6])("assigns a navigable anchor to heading level %s", (level) => {
    const view = render(<MarkdownRenderer content={`${"#".repeat(level)} 标题\n\n[跳转](#标题)`} isStreaming={isStreaming} />);
    const heading = view.getByRole("heading", { name: "标题", level });
    const scroll = vi.fn();
    heading.scrollIntoView = scroll;
    fireEvent.click(view.getByRole("link", { name: "跳转" }));
    expect(scroll).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(document.activeElement).toBe(heading);
  });

  it("preserves a long inline code span containing backticks and math-looking text", () => {
    const literal = String.raw`code with \[x\] and \(y\) and $$z$$ and ` + "`tick`";
    const view = render(<MarkdownRenderer content={`\`\` ${literal} \`\``} isStreaming={isStreaming} />);
    expect(view.container.querySelector("code")?.textContent).toBe(literal);
    expect(view.container.querySelector(".katex")).toBeNull();
  });

  it("keeps an empty explicit target from becoming a file based on its label", () => {
    const view = render(<MarkdownRenderer content="[README.md]()" isStreaming={isStreaming} knownFilePaths={["README.md"]} />);
    expect(view.container.querySelector(".md-file-chip")).toBeNull();
  });
});

describe("literal code source", () => {
  const literal = String.raw`[文件](C:\projects\demo\app.ts)
$$x$$
\[y\]
\(z\)`;
  const examples = [
    { name: "backtick fence", content: `\`\`\`text\n${literal}\n\`\`\`` },
    { name: "tilde fence", content: `~~~text\n${literal}\n~~~` },
    { name: "long fence containing a short fence", content: `\`\`\`\`text\n${literal}\n\`\`\`\n\`\`\`\`` },
    { name: "quoted fence", content: `> ~~~text\n${literal.split("\n").map((line) => `> ${line}`).join("\n")}\n> ~~~` },
    { name: "indented block", content: literal.split("\n").map((line) => `    ${line}`).join("\n") },
  ];
  it.each(examples)("keeps the original displayed/copied source in a $name", async ({ content, name }) => {
    const view = render(<MarkdownRenderer content={content} />);
    expect(view.container.querySelector("pre code")).not.toBeNull();
    expect(view.container.querySelector(".katex")).toBeNull();
    expect(view.container.textContent).toContain(String.raw`C:\projects\demo\app.ts`);
    expect(view.container.textContent).not.toContain("minicode-local-file:");
    fireEvent.click(view.getByRole("button", { name: "复制" }));
    const expected = name === "long fence containing a short fence" ? `${literal}\n\`\`\`` : literal;
    await waitFor(() => expect(writeClipboard).toHaveBeenCalledWith(expected));
  });
});

it("settles a mixed streamed document to the same structures as a complete document", () => {
  const content = [
    "#### 文档标题", "", "正文 **粗体**、*斜体*、~~删除~~、$x+y$。", "",
    "| 名称 | 文件 |", "| --- | --- |", "| 控制条 | [第 13–14 行](page.html:13–14) |", "",
    "- [ ] 待办", "- [x] 完成", "", "> 引用段落", "",
    "```text", String.raw`$$literal$$ [例子](C:\demo\page.html)`, "```", "",
    "访问 https://example.com 或 [参考][ref]。", "", "[ref]: https://example.com/reference", "",
  ].join("\n");
  const shape = (element: HTMLElement) => Object.fromEntries([
    "h4", "strong", "em", "del", ".katex", "table", "thead", "tbody tr", "input[type=checkbox]",
    "blockquote", "pre", "a", ".md-file-chip", ".md-official-file-icon > svg",
  ].map((selector) => [selector, element.querySelectorAll(selector).length]));
  const complete = render(<MarkdownRenderer content={content} />);
  const expected = shape(complete.container);
  complete.unmount();
  const streamed = render(<MarkdownRenderer content="" isStreaming />);
  for (let step = 1; step <= 8; step++) {
    streamed.rerender(<MarkdownRenderer content={content.slice(0, Math.ceil(content.length * step / 8))} isStreaming />);
  }
  streamed.rerender(<MarkdownRenderer content={content} isStreaming={false} />);
  expect(shape(streamed.container)).toEqual(expected);
  expect(streamed.getByRole("link", { name: "参考" }).getAttribute("href")).toBe("https://example.com/reference");
  expect(streamed.container.querySelector(".katex-error")).toBeNull();
});

it("keeps escaped and entity bold markers literal through streamed settlement", () => {
  const content = ["开始", "", String.raw`\*\*literal\*\* &ast;&ast;entity&ast;&ast;`, "", "前**加粗。**后", "", "结束"].join("\n");
  const streamed = render(<MarkdownRenderer content="" isStreaming />);
  for (let step = 1; step <= 8; step++) {
    streamed.rerender(<MarkdownRenderer content={content.slice(0, Math.ceil(content.length * step / 8))} isStreaming />);
  }
  streamed.rerender(<MarkdownRenderer content={content} isStreaming={false} />);
  expect([...streamed.container.querySelectorAll("p")].map((node) => node.textContent)).toEqual(["开始", "**literal** **entity**", "前加粗。后", "结束"]);
  expect([...streamed.container.querySelectorAll("strong")].map((node) => node.textContent)).toEqual(["加粗。"]);
});
