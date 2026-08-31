/* @vitest-environment jsdom */

import { describe, expect, it } from "vitest";
import { sanitizeColorizedHtml } from "./monaco-colorize";

describe("sanitizeColorizedHtml", () => {
  it("removes executable markup from colorized diff HTML", () => {
    const html = sanitizeColorizedHtml([
      `<span class="mtk1" onclick="evil()" style="color: #fff; background-image: url(javascript:evil)">safe</span>`,
      `<img src=x onerror=evil()>`,
      `<script>alert(1)</script>`,
      `<span style="font-weight: bold; behavior: url(#x)">text</span>`,
    ].join(""));

    expect(html).toContain("safe");
    expect(html).toContain("text");
    expect(html).toContain('class="mtk1"');
    expect(html).toContain("color: #fff");
    expect(html).toContain("font-weight: bold");
    expect(html).not.toContain("onclick");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script");
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain("url(");
  });
});
