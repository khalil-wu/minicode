import { describe, expect, it } from "vitest";
import { compactText } from "./FileChangeRenderer";

describe("FileChangeRenderer compactText", () => {
  it("uses an explicit truncation notice instead of a bare ellipsis", () => {
    const output = compactText("x".repeat(901));
    expect(output).toContain("内容已截断");
    expect(output).not.toMatch(/\n\.\.\.$/);
  });
});
