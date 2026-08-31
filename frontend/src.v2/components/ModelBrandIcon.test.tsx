// @vitest-environment jsdom

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModelBrandIcon, resolveModelBrand } from "./ModelBrandIcon";

describe("ModelBrandIcon", () => {
  it.each([
    ["deepseek-v4-pro", "deepseek"],
    ["anthropic/claude-sonnet-4-6", "claude"],
    ["gpt-5.2-codex", "openai"],
    ["o1", "openai"],
    ["o3", "openai"],
    ["o4", "openai"],
    ["qwen3-coder", "qwen"],
    ["glm-5", "zhipu"],
    ["xiaomi/mimo-v2", "mimo"],
    ["meta-llama/llama-4", "meta"],
  ])("maps %s to the %s brand", (model, expected) => {
    expect(resolveModelBrand(model)?.id).toBe(expected);
  });

  it("uses an official color asset when available", () => {
    const { container } = render(<ModelBrandIcon model="deepseek-chat" size={24} />);
    expect(container.querySelector('[data-model-brand="deepseek"] img')).toBeTruthy();
  });

  it("uses a theme-aware mask for monochrome brand assets", () => {
    const { container } = render(<ModelBrandIcon model="gpt-5" size={24} />);
    expect(container.querySelector('[data-model-brand="openai"] img[data-icon-kind="mono"]')).toBeTruthy();
  });

  it("uses a compact shared frame when a provider card requests one", () => {
    const { container } = render(<ModelBrandIcon model="gemini-2.5-pro" size={20} framed />);
    const icon = container.querySelector('[data-model-brand="gemini"]');
    expect(icon?.classList.contains("model-brand-icon")).toBe(true);
    expect(icon?.classList.contains("model-brand-icon-framed")).toBe(true);
    expect(icon?.querySelector("img")?.getAttribute("width")).toBe("14");
  });

  it("falls back cleanly for custom models", () => {
    const { container } = render(<ModelBrandIcon model="my-private-model" size={24} />);
    const icon = container.querySelector('[data-model-brand="custom"]');
    expect(icon).toBeTruthy();
    expect(icon?.getAttribute("title")).toBeNull();
  });

  it("uses the provider website icon for unknown custom providers", () => {
    const { container } = render(<ModelBrandIcon model="private-model" provider="Private AI" websiteUrl="https://models.example.com/v1" size={24} />);
    expect(container.querySelector('[data-model-brand="custom"] [data-brand="website"] img')?.getAttribute("src")).toBe(
      "https://www.google.com/s2/favicons?domain_url=https%3A%2F%2Fmodels.example.com&sz=64",
    );
  });
});
