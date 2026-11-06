/* @vitest-environment jsdom */
import { expect, it } from "vitest";
import { highlightCode, prepareCodeHighlighting } from "./code-highlighting";

it.each(["html", "javascript", "unknown"])("preserves literal source and blank lines in %s highlight HTML", async language => {
  const source = '<img src=x onerror="alert(1)">\n\n<script>alert("&amp; 中文")</script>\n';
  const lines = await highlightCode(source, language, "codex", "light");
  const code = document.createElement("code");
  code.innerHTML = lines.join("\n");
  expect(code.textContent).toBe(source);
  expect(lines).toHaveLength(4);
  expect(code.querySelector("img, script, [onerror]")).toBeNull();
  expect([...code.querySelectorAll("*")].every(element => element.tagName === "SPAN")).toBe(true);
});

it.each([
  ["typescript", 'const value = 1 + 2;\nconsole.log("<value>");\n'],
  ["python", 'def add(a, b):\n    return a + b  # 注释\n'],
  ["html", '<div class="value">&amp; 中文</div>\n'],
] as const)("retains exact text and per-character colors when consolidating %s token runs", async (language, source) => {
  const { highlighter, loaded, lang } = await prepareCodeHighlighting("codex", "light", language);
  const tokens = highlighter.codeToTokensBase(source, { lang, theme: loaded.registrationName });
  const lines = await highlightCode(source, language, "codex", "light");
  let spans = 0;
  lines.forEach((html, index) => {
    const code = document.createElement("code");
    code.innerHTML = html;
    const expected = tokens[index].flatMap(token => {
      const element = document.createElement("span");element.style.color = token.color ?? "";
      return [...token.content].map(character => [character, character.trim() ? element.style.color : ""]);
    });
    const rendered = [...code.children].flatMap(element => [...element.textContent!].map(character => [character, character.trim() ? (element as HTMLElement).style.color : ""]));
    expect(rendered).toEqual(expected);
    spans += code.children.length;
  });
  expect(spans).toBeLessThan(tokens.flat().length);
});
