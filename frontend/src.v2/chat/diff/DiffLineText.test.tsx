/* @vitest-environment jsdom */
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DiffLineText } from "./DiffLineText";

afterEach(cleanup);

it("preserves plain source bytes, escaped markup and UTF-16 word offsets while switching files", () => {
  const first = '😀 old <img src=x onerror="bad()"> & tail';
  const view = render(<DiffLineText text={first} textKey="0:old" spans={[{ start: 3, end: 6 }]} />);
  expect(view.container.textContent).toBe(first);
  expect(view.container.querySelector("img")).toBeNull();
  expect(view.container.querySelector("[data-diff-span]")?.textContent).toBe("old");
  const second = "\tnew\tvalue  ";
  view.rerender(<DiffLineText text={second} textKey="2:new" spans={[{ start: 1, end: 4 }, { start: 4, end: 5 }]} />);
  expect(view.container.textContent).toBe(second);
  expect([...view.container.querySelectorAll("[data-diff-span]")].map(node => node.textContent)).toEqual(["new", "\t"]);
  expect(view.container.querySelector('[data-diff-text="0:old"]')).toBeNull();
});

it("switches syntax highlighting and word decorations without retaining the previous file markup", () => {
  const view = render(<DiffLineText text="old value" textKey="0:new" spans={[{ start: 0, end: 3 }]}
    html={'<span class="keyword" style="color: red">old</span> value'} />);
  expect(view.container.querySelector(".keyword")?.textContent).toBe("old");
  expect(view.container.querySelector("[data-diff-span]")?.textContent).toBe("old");
  view.rerender(<DiffLineText text="current plain" textKey="0:new" spans={[]} />);
  expect(view.container.textContent).toBe("current plain");
  expect(view.container.querySelector(".keyword")).toBeNull();
  expect(view.container.querySelector("[data-diff-span]")).toBeNull();
});
