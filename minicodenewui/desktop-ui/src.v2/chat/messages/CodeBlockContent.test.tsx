/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { CodeBlockContent } from "./CodeBlockContent";
import { TranscriptSearchContext } from "../TranscriptSearchContext";

afterEach(cleanup);

const visibility = (group: HTMLElement, skipped: boolean) => act(() => {
  const event = new Event("contentvisibilityautostatechange");
  Object.defineProperty(event, "skipped", { value: skipped });
  group.dispatchEvent(event);
});

it("applies the latest worker colors when an offscreen group becomes visible and retains its source throughout", () => {
  const text = Array.from({ length: 130 }, (_, index) => `line ${index}`).join("\n");
  const colors = (color: string) => text.split("\n").map(line => `<span style="color: ${color}">${line}</span>`);
  const view = render(<code><CodeBlockContent text={text} /></code>);
  const groups = [...view.container.querySelectorAll<HTMLElement>(".code-line-group")];
  const firstLine = groups[0].firstElementChild;
  visibility(groups[0], true);
  view.rerender(<code><CodeBlockContent text={text} highlightedLines={colors("#123456")} /></code>);
  expect(groups[0].querySelector("span[style]")).toBeNull();
  expect(groups[0].firstElementChild).toBe(firstLine);
  expect(groups[0].textContent).toContain("line 63");
  expect(groups[1].querySelector("span[style]")).not.toBeNull();
  view.rerender(<code><CodeBlockContent text={text} highlightedLines={colors("#abcdef")} /></code>);
  visibility(groups[0], false);
  expect(groups[0].querySelector<HTMLElement>("span[style]")?.style.color).toBe("rgb(171, 205, 239)");
  const coloredLine = groups[0].firstElementChild;
  visibility(groups[0], true);
  expect(groups[0].firstElementChild).toBe(coloredLine);
  view.rerender(<code><CodeBlockContent text="replacement" /></code>);
  expect(groups[0].textContent).toBe("1replacement");
  expect(groups[0].querySelector("span[style]")).toBeNull();
  expect(view.container.querySelectorAll(".code-theme-line-number")).toHaveLength(1);
});

it("keeps selected code nodes until the user releases the selection, then applies current colors", () => {
  const text = "const selected = 1;\nconst next = 2;";
  const view = render(<code><CodeBlockContent text={text} /></code>);
  const group = view.container.querySelector<HTMLElement>(".code-line-group")!;
  const node = group.firstElementChild!.lastChild!;
  const range = document.createRange();range.selectNodeContents(node);
  const selection = document.getSelection()!;selection.removeAllRanges();selection.addRange(range);
  const lines = (color: string) => text.split("\n").map(line => `<span style="color: ${color}">${line}</span>`);
  view.rerender(<code><CodeBlockContent text={text} highlightedLines={lines("#123456")} /></code>);
  expect(node.isConnected).toBe(true);
  expect(selection.toString()).toBe("const selected = 1;");
  view.rerender(<code><CodeBlockContent text={text} highlightedLines={lines("#abcdef")} /></code>);
  expect(group.querySelector("span[style]")).toBeNull();
  act(() => { selection.removeAllRanges(); document.dispatchEvent(new Event("selectionchange")); });
  expect(group.querySelector<HTMLElement>("span[style]")?.style.color).toBe("rgb(171, 205, 239)");
  expect(group.textContent).toContain("const next = 2;");
});

it.each(["search", "print"])("applies pending colors to skipped source before %s exposes it", mode => {
  const content = (highlighted: boolean, searching = false) => <TranscriptSearchContext.Provider value={searching}>
    <code><CodeBlockContent text="plain source" highlightedLines={highlighted ? ['<span style="color: #123456">plain source</span>'] : undefined} /></code>
  </TranscriptSearchContext.Provider>;
  const view = render(content(false));
  const group = view.container.querySelector<HTMLElement>(".code-line-group")!;
  visibility(group, true);
  view.rerender(content(true));
  expect(group.querySelector("span[style]")).toBeNull();
  if (mode === "search") view.rerender(content(true, true));
  else act(() => {
    window.dispatchEvent(new Event("beforeprint"));
    expect(group.querySelector("span[style]")).not.toBeNull();
  });
  expect(group.querySelector<HTMLElement>("span[style]")?.style.color).toBe("rgb(18, 52, 86)");
});

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
