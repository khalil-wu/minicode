import { beforeEach, describe, expect, it, vi } from "vitest";

const openMocks = vi.hoisted(() => ({
  browser: vi.fn(() => true),
  preview: vi.fn(() => true),
}));

vi.mock("./openWebInBrowser", () => ({ openWebInBrowser: openMocks.browser }));
vi.mock("./openWebInPreview", () => ({ openWebInPreview: openMocks.preview }));

import { openWebTarget } from "./openWebTarget";

describe("openWebTarget", () => {
  beforeEach(() => vi.clearAllMocks());

  it("routes local web links to Browser rather than Preview", () => {
    expect(openWebTarget("http://localhost:4173/app")).toBe(true);

    expect(openMocks.browser).toHaveBeenCalledWith("http://localhost:4173/app");
    expect(openMocks.preview).not.toHaveBeenCalled();
  });

  it("routes ordinary web pages and sources to Browser", () => {
    expect(openWebTarget("https://openai.com/research")).toBe(true);

    expect(openMocks.browser).toHaveBeenCalledWith("https://openai.com/research");
    expect(openMocks.preview).not.toHaveBeenCalled();
  });
});
