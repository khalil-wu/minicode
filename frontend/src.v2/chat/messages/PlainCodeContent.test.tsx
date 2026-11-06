/* @vitest-environment jsdom */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { PlainCodeContent } from "./PlainCodeContent";

afterEach(cleanup);

it.each([10, 64, 80])("preserves code nodes and selection while a %i-line fence grows", count => {
  const first = Array.from({ length: count }, (_, index) => `const value${index} = ${index};`).join("\n");
  const view = render(<code><PlainCodeContent text={first} /></code>);
  const line = view.container.querySelectorAll(".block")[count - 1];
  const text = line.lastChild!;
  const range = document.createRange();range.selectNodeContents(text);
  const selection = window.getSelection()!;selection.removeAllRanges();selection.addRange(range);
  view.rerender(<code><PlainCodeContent text={`${first}\nconst latest = 80;`} /></code>);
  expect(view.container.querySelectorAll(".block")[count - 1]).toBe(line);
  expect(line.lastChild).toBe(text);
  expect(selection.toString()).toBe(`const value${count - 1} = ${count - 1};`);
  expect([...view.container.querySelectorAll(".code-theme-line-number")].map(node => node.textContent))
    .toEqual(Array.from({ length: count + 1 }, (_, index) => String(index + 1)));
  selection.removeAllRanges();
});

it("preserves literal source, blank lines and replacements without interpreting markup", () => {
  const source = '<img src=x onerror="evil()">\r\n\n<script>"&amp; 中文"</script>\n';
  const view = render(<code><PlainCodeContent text={source} /></code>);
  const content = () => [...view.container.querySelectorAll(".block")].map(line => [...line.childNodes].slice(1).map(node => node.textContent).join("")).join("\n");
  expect(content()).toBe(source);
  expect(view.container.querySelector("img,script,[onerror]")).toBeNull();
  view.rerender(<code><PlainCodeContent text="replacement" /></code>);
  expect(content()).toBe("replacement");
  expect(view.container.querySelectorAll(".code-theme-line-number")).toHaveLength(1);
});
