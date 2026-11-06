/* @vitest-environment jsdom */
import { expect, it } from "vitest";
import { highlightCode } from "./code-highlighting";

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
