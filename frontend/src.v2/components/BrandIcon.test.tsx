// @vitest-environment jsdom

import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { BrandIcon, resolveBrandIcon, resolveWebsiteBrandIcon, resolveWebsiteIcon, resolveWebsiteIconCandidates } from "./BrandIcon";

// Vitest stubs CSS module imports, including ?raw. Load the same tracked
// stylesheet sources explicitly for this DOM cascade regression.
const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const brandStyles = readFileSync(resolve(sourceDirectory, "BrandIcon.css"), "utf8");
const markdownStyles = readFileSync(resolve(sourceDirectory, "../styles/utilities.css"), "utf8");

describe("BrandIcon", () => {
  it.each([
    ["deepseek-chat", "DeepSeek"],
    ["ChatGPT", "OpenAI"],
    ["github", "GitHub"],
    ["figma-desktop", "Figma"],
    ["@playwright/mcp", "Playwright"],
    ["Google Drive", "Google Drive"],
    ["google-drive", "Google Drive"],
  ])("uses the official icon for %s", (value, label) => {
    expect(resolveBrandIcon(value)?.label).toBe(label);
  });

  it("keeps a neutral fallback for unknown integrations", () => {
    const { container } = render(<BrandIcon value="internal-tool" fallback="plugin" />);
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
  });

  it("applies monochrome theme treatment only to known monochrome assets", () => {
    const view = render(<BrandIcon value="OpenAI" />);
    expect(view.container.querySelector('img.brand-icon-image[data-icon-kind="mono"]')).toBeTruthy();
    view.rerender(<BrandIcon value="Claude" />);
    expect(view.container.querySelector('img.brand-icon-image[data-icon-kind="color"]')).toBeTruthy();
    view.rerender(<BrandIcon value="OpenAI" iconUrl="https://example.com/extension-color.svg" />);
    expect(view.container.querySelector('img')?.hasAttribute("data-icon-kind")).toBe(false);
  });

  it("keeps a local website icon visible while the actual site's favicon is still pending", () => {
    expect(resolveWebsiteIcon(undefined, "https://example.com/docs/start")).toBe(
      "https://example.com/favicon.ico",
    );
    const { container } = render(<BrandIcon value="Example Docs" websiteUrl="https://example.com/docs" fallback="web" />);
    const image = container.querySelector<HTMLImageElement>(".brand-icon-remote")!;
    expect(image.getAttribute("src")).toBe("https://example.com/favicon.ico");
    expect(image.hidden).toBe(true);
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
    fireEvent.load(image);
    expect(image.hidden).toBe(false);
    expect(container.querySelector('[data-brand="website"] img')).toBe(image);
    expect(container.querySelector("svg")).toBeNull();
  });

  it.each([
    "https://news.bjd.com.cn/report?utm_source=openai",
    "https://example.com/articles/claude?ref=github",
  ])("identifies a web source by its hostname, ignoring article text and tracking parameters: %s", (websiteUrl) => {
    const { container } = render(<BrandIcon value={`OpenAI Claude GitHub ${websiteUrl}`} websiteUrl={websiteUrl} fallback="web" />);
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
    expect(container.querySelector<HTMLImageElement>(".brand-icon-remote")?.src).toBe(new URL("/favicon.ico", websiteUrl).toString());
    expect(container.querySelector(".brand-icon-image")).toBeNull();
  });

  it("retains the local icon through failed declared logos and site favicons", () => {
    expect(resolveWebsiteIconCandidates("https://cdn.example.com/icon.svg", "https://example.com/docs")).toEqual([
      "https://cdn.example.com/icon.svg",
      "https://example.com/favicon.ico",
    ]);
    const { container } = render(<BrandIcon value="Unknown MCP" iconUrl="https://cdn.example.com/icon.svg" websiteUrl="https://example.com/docs" />);
    const image = container.querySelector('.brand-icon-remote');
    expect(image).toBeTruthy();
    fireEvent.error(image as HTMLImageElement);
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
    expect(container.querySelector('.brand-icon-remote')?.getAttribute("src")).toBe("https://example.com/favicon.ico");
    fireEvent.error(container.querySelector('.brand-icon-remote') as HTMLImageElement);
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
  });

  it("prefers the declared extension icon over name-based brand inference", () => {
    const { container } = render(<BrandIcon value="GitHub" iconUrl="https://example.com/icon.svg" />);
    expect(container.querySelector('img[src="https://example.com/icon.svg"]')).toBeTruthy();
    expect(container.querySelector('[data-brand="github"] svg')).toBeTruthy();
    fireEvent.load(container.querySelector("img")!);
    expect(container.querySelector('[data-brand="website"] img')?.hasAttribute("hidden")).toBe(false);
    fireEvent.error(container.querySelector("img")!);
    expect(container.querySelector('[data-brand="github"] svg')).toBeTruthy();
  });

  it.each([
    ["https://api.openai.com/v1", "OpenAI"], ["https://github.com/openai/codex", "GitHub"],
    ["http://github.com/openai/codex", "GitHub"], ["https://drive.google.com/files", "Google Drive"],
    ["https://gemini.google.com/app", "Google Gemini"], ["https://example.com/openai?ref=github.com", null],
    ["https://github.com.other.example", null], ["mailto:person@github.com", null],
  ])("uses the registered website identity for %s", (url, label) => {
    expect(resolveWebsiteBrandIcon(url)?.label ?? null).toBe(label);
  });

  it("keeps the bundled host identity instead of an unrelated third-party profile logo", () => {
    const { container } = render(<BrandIcon value="OpenAI article" websiteUrl="https://github.com/openai" iconUrl="https://profiles.example/avatar.png" fallback="web" />);
    expect(container.querySelector('[data-brand="github"] svg')).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
  });

  it("preserves monochrome asset contrast inside Markdown links in both themes", () => {
    const priorTheme = document.documentElement.getAttribute("data-theme");
    const style = document.createElement("style");
    style.textContent = `${brandStyles}\n${markdownStyles.replace(/^@tailwind [^;]+;/gm, "")}`;
    document.head.append(style);
    const { container } = render(<div className="md-body"><BrandIcon value="OpenAI" websiteUrl="https://openai.com" fallback="web" className="md-web-link-icon" /></div>);
    const icon = container.querySelector('img[data-icon-kind="mono"]')!;
    try {
      document.documentElement.setAttribute("data-theme", "dark");
      expect(getComputedStyle(icon).filter).toBe("invert(1)");
      document.documentElement.setAttribute("data-theme", "light");
      expect(getComputedStyle(icon).filter).toBe("none");
    } finally {
      style.remove();
      if (priorTheme === null) document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", priorTheme);
    }
  });

  it("does not mistake a skill name for its publisher", () => {
    const { container } = render(<BrandIcon value="GitHub review" inferBrand={false} fallback="skill" />);
    expect(container.querySelector('[data-brand="generic"]')).toBeTruthy();
  });

  it("accepts a same-origin bundled asset and switches to a replacement icon", () => {
    const { container, rerender } = render(<BrandIcon value="OpenAI Docs" iconUrl="/api/skills/asset?variant=small" />);
    expect(container.querySelector("img")?.getAttribute("src")).toContain("/api/skills/asset?variant=small");
    fireEvent.error(container.querySelector("img")!);
    rerender(<BrandIcon value="OpenAI Docs" iconUrl="/api/skills/asset?variant=large" />);
    expect(container.querySelector("img")?.getAttribute("src")).toContain("variant=large");
  });

  it("separates replaced resource elements and resets failures for the new resource scope", () => {
    const view = render(<BrandIcon value="Unknown" iconUrl="https://example.com/old.svg" />);
    const old = view.container.querySelector("img")!;
    view.rerender(<BrandIcon value="Unknown" iconUrl="https://example.com/new.svg" />);
    expect(view.container.querySelector("img")).not.toBe(old);
    fireEvent.error(old);
    expect(view.container.querySelector("img")?.getAttribute("src")).toBe("https://example.com/new.svg");
    fireEvent.error(view.container.querySelector("img")!);
    expect(view.container.querySelector('[data-brand="generic"]')).toBeTruthy();
    view.rerender(<BrandIcon value="Unknown" iconUrl="https://example.com/old.svg" />);
    expect(view.container.querySelector("img")?.getAttribute("src")).toBe("https://example.com/old.svg");
  });

  it("requires a new load after a previously loaded icon leaves and returns to the slot", () => {
    const view = render(<BrandIcon value="Unknown" iconUrl="https://example.com/a.svg" />);
    fireEvent.load(view.container.querySelector("img")!);
    expect(view.container.querySelector<HTMLImageElement>("img")?.hidden).toBe(false);
    view.rerender(<BrandIcon value="Unknown" iconUrl="https://example.com/b.svg" />);
    view.rerender(<BrandIcon value="Unknown" iconUrl="https://example.com/a.svg" />);
    const current = view.container.querySelector<HTMLImageElement>("img")!;
    expect(current.hidden).toBe(true);
    expect(view.container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
    fireEvent.load(current);
    expect(current.hidden).toBe(false);
  });
});
