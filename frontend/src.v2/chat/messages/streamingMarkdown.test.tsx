/* @vitest-environment jsdom */
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StreamingMarkdownPartition } from "./streamingMarkdown";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { fromMarkdown } from "mdast-util-from-markdown";

vi.mock("mdast-util-from-markdown", async load => {
  const actual = await load<typeof import("mdast-util-from-markdown")>();
  return { ...actual, fromMarkdown: vi.fn(actual.fromMarkdown) };
});

afterEach(() => { cleanup(); vi.mocked(fromMarkdown).mockClear(); });

it("scans completed lines once and preserves list/fence/math blocks", () => {
  const value = "# Title\n\n- first\n\n- second\n\nNext\n\n```ts\nconst x = 1;\n\nconst y = 2;\n```\n\n$$\na+b\n\nc+d\n$$\n\nEnd";
  const partition = new StreamingMarkdownPartition();
  let view = partition.push("");
  for (let i = 1; i <= value.length; i++) view = partition.push(value.slice(0, i));
  expect(partition.scannedCharacters).toBeLessThanOrEqual(value.length);
  expect(view.parts.some((part) => part.content.includes("- first") && part.content.includes("- second"))).toBe(true);
  expect(view.parts.some((part) => part.content.includes("const x") && part.content.includes("const y"))).toBe(true);
  expect(view.parts.some((part) => part.content.includes("a+b") && part.content.includes("c+d"))).toBe(true);
});

it("keeps old completed blocks mounted when a new completed paragraph arrives", () => {
  const first = "## Heading\n\nFirst **paragraph**.\n\nSecond";
  const { container, rerender } = render(<MarkdownRenderer content={first} isStreaming />);
  const heading = container.querySelector("h2");
  const paragraph = container.querySelector("p");
  rerender(<MarkdownRenderer content={`${first} completed.\n\nThird paragraph`} isStreaming />);
  expect(container.querySelector("h2")).toBe(heading);
  expect(container.querySelector("p")).toBe(paragraph);
  expect(container.textContent).toContain("Second completed.");
});

it("resolves later reference definitions across completed chunks", () => {
  const { rerender } = render(<MarkdownRenderer content={"Earlier [reference][ref].\n\nAnother paragraph"} isStreaming />);
  rerender(<MarkdownRenderer content={"Earlier [reference][ref].\n\nAnother paragraph\n\n[ref]: https://example.com\n"} isStreaming />);
  expect(screen.getByRole("link", { name: "reference" }).getAttribute("href")).toBe("https://example.com");
});

it("resets partition ownership when the source is replaced", () => {
  const partition = new StreamingMarkdownPartition();
  partition.push("Old paragraph\n\nOld tail");
  const view = partition.push("Replacement\n\nNew tail");
  expect(view.parts.map((part) => part.content).join("") + view.tail.content).toBe("Replacement\n\nNew tail");
});

it("preserves a multi-paragraph list as one list during streaming", () => {
  const { container } = render(<MarkdownRenderer content={"- first\n\n- second\n\nOutside list\n\n"} isStreaming />);
  expect(container.querySelectorAll("ul")).toHaveLength(1);
  expect(container.querySelectorAll("li")).toHaveLength(2);
});

it.each(["closed", "settled"] as const)("keeps an open code fence literal without parsing it until %s", outcome => {
  const code = [String.raw`const path = "C:\work\file.ts";`,
    String.raw`const literal = "$$price$$ [1] \(raw\)";`, "```"].join("\n");
  const source = "````typescript\n" + code;
  const view = render(<MarkdownRenderer content={source} isStreaming />);
  expect(fromMarkdown).not.toHaveBeenCalled();
  expect(view.container.querySelector("pre")?.getAttribute("data-markdown-copy-text")).toBe(code);
  const continued = code + "\nconst tail = '$literal';";
  view.rerender(<MarkdownRenderer content={"````typescript\n" + continued} isStreaming />);
  expect(fromMarkdown).not.toHaveBeenCalled();
  expect(view.container.querySelector("pre")?.getAttribute("data-markdown-copy-text")).toBe(continued);

  view.rerender(<MarkdownRenderer content={"````typescript\n" + continued + (outcome === "closed" ? "\n````\n\nOutside \\(x+y\\)" : "")}
    isStreaming={outcome === "closed"} />);
  expect(fromMarkdown).toHaveBeenCalled();
  expect(view.container.querySelector("[data-markdown-copy-text]")?.getAttribute("data-markdown-copy-text")).toBe(continued);
  if (outcome === "closed") expect(view.container.querySelector(".katex")).not.toBeNull();
});
