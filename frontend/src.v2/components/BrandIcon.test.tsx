// @vitest-environment jsdom

import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BrandIcon, resolveBrandIcon, resolveWebsiteIcon, resolveWebsiteIconCandidates } from "./BrandIcon";

describe("BrandIcon", () => {
  it.each([
    ["deepseek-chat", "DeepSeek"],
    ["ChatGPT", "OpenAI"],
    ["github", "GitHub"],
    ["figma-desktop", "Figma"],
    ["@playwright/mcp", "Playwright"],
  ])("uses the official icon for %s", (value, label) => {
    expect(resolveBrandIcon(value)?.label).toBe(label);
  });

  it("keeps a neutral fallback for unknown integrations", () => {
    const { container } = render(<BrandIcon value="internal-tool" fallback="plugin" />);
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
  });

  it("uses a website favicon when no official brand icon is known", () => {
    expect(resolveWebsiteIcon(undefined, "https://example.com/docs/start")).toBe(
      "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2Fexample.com&sz=64",
    );
    const { container } = render(<BrandIcon value="Example Docs MCP" websiteUrl="https://example.com/docs" />);
    expect(container.querySelector('[data-brand="website"] img')?.getAttribute("src")).toBe(
      "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2Fexample.com&sz=64",
    );
  });

  it("tries domain discovery and the site favicon before the neutral icon", () => {
    expect(resolveWebsiteIconCandidates("https://cdn.example.com/icon.svg", "https://example.com/docs")).toEqual([
      "https://cdn.example.com/icon.svg",
      "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2Fexample.com&sz=64",
      "https://example.com/favicon.ico",
    ]);
    const { container } = render(<BrandIcon value="Unknown MCP" iconUrl="https://cdn.example.com/icon.svg" websiteUrl="https://example.com/docs" />);
    const image = container.querySelector('[data-brand="website"] img');
    expect(image).toBeTruthy();
    fireEvent.error(image as HTMLImageElement);
    expect(container.querySelector('[data-brand="website"] img')?.getAttribute("src")).toBe(
      "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2Fexample.com&sz=64",
    );
    fireEvent.error(container.querySelector('[data-brand="website"] img') as HTMLImageElement);
    expect(container.querySelector('[data-brand="website"] img')?.getAttribute("src")).toBe("https://example.com/favicon.ico");
    fireEvent.error(container.querySelector('[data-brand="website"] img') as HTMLImageElement);
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
  });

  it("prefers bundled official icons over remote website icons", () => {
    const { container } = render(<BrandIcon value="GitHub" iconUrl="https://example.com/icon.svg" />);
    expect(container.querySelector('[data-brand="github"] svg')).toBeTruthy();
    expect(container.querySelector('img[src="https://example.com/icon.svg"]')).toBeNull();
  });
});
