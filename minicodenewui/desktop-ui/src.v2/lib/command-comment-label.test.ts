import { describe, expect, it } from "vitest";
import { extractCommandCommentLabel } from "./command-comment-label";

describe("extractCommandCommentLabel", () => {
  it("extracts a leading # comment as the label", () => {
    expect(extractCommandCommentLabel("# install deps\nnpm install")).toBe("install deps");
  });

  it("strips multiple leading hashes", () => {
    expect(extractCommandCommentLabel("## build step\nmake build")).toBe("build step");
  });

  it("ignores shebang lines", () => {
    expect(extractCommandCommentLabel("#!/bin/bash\necho hi")).toBeUndefined();
  });

  it("returns undefined when there is no leading comment", () => {
    expect(extractCommandCommentLabel("npm install")).toBeUndefined();
    expect(extractCommandCommentLabel("git status\n# later")).toBeUndefined();
  });

  it("handles empty / whitespace-only input", () => {
    expect(extractCommandCommentLabel("")).toBeUndefined();
    expect(extractCommandCommentLabel(undefined)).toBeUndefined();
  });

  it("ignores a comment that is only hashes", () => {
    expect(extractCommandCommentLabel("#\necho hi")).toBeUndefined();
  });
});
