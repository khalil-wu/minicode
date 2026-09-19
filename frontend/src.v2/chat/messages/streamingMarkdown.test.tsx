/* @vitest-environment jsdom */
import { afterEach, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StreamingMarkdownPartition } from "./streamingMarkdown";
import { MarkdownRenderer } from "./MarkdownRenderer";

afterEach(cleanup);

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
