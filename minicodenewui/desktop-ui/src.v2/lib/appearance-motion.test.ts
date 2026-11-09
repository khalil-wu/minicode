/* @vitest-environment jsdom */
import { readFileSync } from "node:fs";
import postcss from "postcss";
import { afterEach, expect, it } from "vitest";
import { useAppStore } from "../stores";
import files from "../../test/appearance-motion-files.json";

afterEach(() => document.querySelectorAll("[data-motion-fixture]").forEach(element => element.remove()));

it("uses the real tooltip rules so explicit Off keeps animation even when the OS requests reduced motion", () => {
  const tooltip = postcss.parse(readFileSync("src.v2/components/Tooltip.css", "utf8"));
  // This fixture represents the browser having matched OS reduce=true. The
  // real reduced-motion selectors and declarations are used without edits.
  tooltip.walkAtRules("media", media => { if (media.params.includes("prefers-reduced-motion")) media.replaceWith(...media.nodes!); });
  const style = document.createElement("style"); style.dataset.motionFixture = "true"; style.textContent = tooltip.toString(); document.head.appendChild(style);
  const hint = document.createElement("span"); hint.dataset.motionFixture = "true"; hint.className = "mc-tip-bubble"; document.body.appendChild(hint);
  useAppStore.getState().setReducedMotionPreference("off");
  expect(getComputedStyle(hint).animation).toContain("mc-tip-enter");
  useAppStore.getState().setReducedMotionPreference("on");
  expect(getComputedStyle(hint).animation).toBe("none");
});

it("keeps every audited OS media rule conditional on the explicit application preference", () => {
  expect(files).toContain("frontend/src.v2/panels/BrowserPanel.css");
  expect(files).toContain("frontend/src.v2/composer/composer.css");
  for (const file of files) {
    const style = postcss.parse(readFileSync(`../${file}`, "utf8"));
    style.walkAtRules("media", media => {
      if (!/prefers-reduced-motion\s*:\s*reduce/.test(media.params)) return;
      media.walkRules(rule => expect(rule.selector, file).toContain('data-reduced-motion="false"'));
    });
  }
});
