/* @vitest-environment jsdom */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { CodeBlockContent } from "./CodeBlockContent";

afterEach(cleanup);

it.each([10, 64, 80])("preserves code nodes and selection while a %i-line fence grows", count => {
  const first = Array.from({ length: count }, (_, index) => `const value${index} = ${index};`).join("\n");
  const view = render(<code><CodeBlockContent text={first} /></code>);
  const line = view.container.querySelectorAll(".code-line-group > .block")[count - 1];
  const text = line.lastChild!;
  const range = document.createRange();range.selectNodeContents(text);
  const selection = window.getSelection()!;selection.removeAllRanges();selection.addRange(range);
  view.rerender(<code><CodeBlockContent text={`${first}\nconst latest = 80;`} /></code>);
  expect(view.container.querySelectorAll(".code-line-group > .block")[count - 1]).toBe(line);
  expect(line.lastChild).toBe(text);
  expect(selection.toString()).toBe(`const value${count - 1} = ${count - 1};`);
  expect([...view.container.querySelectorAll(".code-theme-line-number")].map(node => node.textContent))
    .toEqual(Array.from({ length: count + 1 }, (_, index) => String(index + 1)));
  selection.removeAllRanges();
});

it("preserves literal source, blank lines and replacements without interpreting markup", () => {
  const source = '<img src=x onerror="evil()">\r\n\n<script>"&amp; 中文"</script>\n';
  const view = render(<code><CodeBlockContent text={source} /></code>);
  const content = () => [...view.container.querySelectorAll(".code-line-group > .block")].map(line => [...line.childNodes].slice(1).map(node => node.textContent).join("")).join("\n");
  expect(content()).toBe(source);
  expect(view.container.querySelector("img,script,[onerror]")).toBeNull();
  view.rerender(<code><CodeBlockContent text="replacement" /></code>);
  expect(content()).toBe("replacement");
  expect(view.container.querySelectorAll(".code-theme-line-number")).toHaveLength(1);
});

it("keeps measured line groups and gutter order when worker highlighting arrives", () => {
  const text = Array.from({ length: 130 }, (_, index) => `line ${index}`).join("\n");
  const view = render(<code><CodeBlockContent text={text} /></code>);
  const groups = [...view.container.querySelectorAll<HTMLElement>(".code-line-group")];
  groups[0].dataset.measured = "true";
  groups[0].style.setProperty("--markdown-block-height", "1400px");
  const highlightedLines = text.split("\n").map(line => `<span style="color: #123456">${line}</span>`);
  view.rerender(<code><CodeBlockContent text={text} highlightedLines={highlightedLines} /></code>);
  expect([...view.container.querySelectorAll(".code-line-group")]).toEqual(groups);
  expect(groups[0].style.getPropertyValue("--markdown-block-height")).toBe("1400px");
  expect(groups[0].dataset.measured).toBe("true");
  expect([...view.container.querySelectorAll(".code-theme-line-number")].map(node => node.textContent))
    .toEqual(Array.from({ length: 130 }, (_, index) => String(index + 1)));
  expect(view.container.querySelectorAll(".code-line-group > .block")[129].lastChild!.textContent).toBe("line 129");
});
