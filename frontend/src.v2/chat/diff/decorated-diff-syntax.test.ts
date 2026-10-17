/* @vitest-environment jsdom */
import { describe, expect, it } from "vitest";
import { decorateDiffLineHtml } from "./decorated-diff-syntax";

function rendered(text: string, html: string | undefined, spans: { start: number; end: number }[]) {
  const body = document.createElement("div");
  body.innerHTML = decorateDiffLineHtml(text, html, spans);
  return body;
}
const changed = (body: HTMLElement) => [...body.querySelectorAll<HTMLElement>("[data-diff-span]")].map(node => node.textContent);

describe("Diff syntax and word decoration merging", () => {
  it("escapes source code on the pending/plain path without making markup executable", () => {
    const code = '<img src=x onerror="alert(1)"> & </span>';
    const body = rendered(code, undefined, [{ start: 1, end: 4 }]);
    expect(body.textContent).toBe(code);
    expect(body.querySelector("img")).toBeNull();
    expect(changed(body)).toEqual(["img"]);
    expect(rendered(code, "", []).textContent).toBe(code);
  });

  it("decorates decoded entities at source offsets and retains syntax styles/classes", () => {
    const body = rendered("<tag> & value", '<span class="mtk1" style="color: #123456">&lt;tag&gt; &amp; value</span>', [{ start: 0, end: 5 }, { start: 8, end: 13 }]);
    expect(body.textContent).toBe("<tag> & value");
    expect(body.querySelector("tag")).toBeNull();
    expect(body.querySelector<HTMLElement>(".mtk1")?.style.color).toBe("rgb(18, 52, 86)");
    expect(changed(body)).toEqual(["<tag>", "value"]);
  });

  it("splits text nodes across nested tokens without replacing their element structure", () => {
    const body = rendered("const answer = 42;", '<span class="outer"><span class="keyword">const</span> <span style="color: red">answer</span> = <span class="number">42</span>;</span>', [{ start: 6, end: 17 }]);
    expect(body.textContent).toBe("const answer = 42;");
    expect(body.querySelectorAll(".outer")).toHaveLength(1);
    expect(body.querySelectorAll(".keyword")).toHaveLength(1);
    expect(body.querySelectorAll(".number")).toHaveLength(1);
    expect(changed(body).join("")).toBe("answer = 42");
  });

  it("keeps astral characters intact and uses UTF-16 offsets", () => {
    const body = rendered("😀 old", '<span>😀 old</span>', [{ start: 3, end: 6 }]);
    expect(body.textContent).toBe("😀 old");
    expect(changed(body)).toEqual(["old"]);
  });

  it("maps Monaco's expanded tabs and NBSP to original code offsets", () => {
    const code = "\talpha\t=\told;";
    const html = '<span><span class="mtk1">\u00a0\u00a0alpha\u00a0=\u00a0old;</span></span>';
    const body = rendered(code, html, [{ start: 9, end: 12 }]);
    expect(body.textContent).toBe("\u00a0\u00a0alpha\u00a0=\u00a0old;");
    expect(changed(body)).toEqual(["old"]);
    const tab = rendered("\told", '<span>\u00a0\u00a0old</span>', [{ start: 0, end: 1 }]);
    expect(changed(tab)).toEqual(["\u00a0\u00a0"]);
  });

  it("retains raw Shiki tabs and highlights adjacent final word-alt parts independently", () => {
    const body = rendered("\ta.b c", '<span style="color: #abcdef">\ta.b c</span>', [{ start: 1, end: 5 }, { start: 5, end: 6 }]);
    expect(body.textContent).toBe("\ta.b c");
    expect(changed(body)).toEqual(["a.b ", "c"]);
    expect(body.querySelector<HTMLElement>("[style]")?.style.color).toBe("rgb(171, 205, 239)");
  });
});
