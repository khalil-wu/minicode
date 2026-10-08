// @vitest-environment jsdom

import { fireEvent, render } from "@testing-library/react";
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

  it("uses shared theme treatment for monochrome brand assets", () => {
    const { container } = render(<ModelBrandIcon model="gpt-5" size={24} />);
    expect(container.querySelector('[data-model-brand="openai"] img.brand-icon-image[data-icon-kind="mono"]')).toBeTruthy();
  });

  it("shows the service identity on provider cards and model identity in model menus", () => {
    const view = render(<ModelBrandIcon model="gpt-5" provider="OpenRouter" entity="provider" />);
    expect(view.container.querySelector('[data-model-brand="openrouter"]')).toBeTruthy();
    view.rerender(<ModelBrandIcon model="gpt-5" provider="OpenRouter" />);
    expect(view.container.querySelector('[data-model-brand="openai"]')).toBeTruthy();
  });

  it("does not impersonate a model vendor for an unknown provider", () => {
    const { container } = render(<ModelBrandIcon model="gpt-5" provider="Private AI" entity="provider" websiteUrl="https://models.example.com/v1" />);
    expect(container.querySelector('[data-model-brand="custom"] [data-brand="generic"] svg')).toBeTruthy();
    expect(container.querySelector('[data-model-brand="openai"]')).toBeNull();
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
    const icon = container.querySelector<HTMLImageElement>('.brand-icon-remote')!;
    expect(icon.src).toBe("https://models.example.com/favicon.ico");
    expect(icon.hidden).toBe(true);
    expect(container.querySelector('[data-model-brand="custom"] svg')).toBeTruthy();
    fireEvent.load(icon);
    expect(container.querySelector('[data-model-brand="custom"] [data-brand="website"] img')).toBe(icon);
    expect(icon.hidden).toBe(false);
  });
});
