import { describe, expect, it } from "vitest";
import { countUnifiedDiffLines, parseDiffGitHeader, parseUnifiedDiffLines, unifiedDiffFilePaths, unquoteGitPath } from "./unified-diff";

describe("unified diff parsing", () => {
  it("counts only hunk lines when structured file patches are joined without diff headers", () => {
    const first = ["--- a/one.ts", "+++ b/one.ts", "@@ -1,2 +1,2 @@", " keep", "-old", "+new"].join("\n");
    const second = ["--- a/two.ts", "+++ b/two.ts", "@@ -1,1 +1,1 @@", "-before", "+after"].join("\n");
    expect(countUnifiedDiffLines(`${first}\n${second}`)).toEqual({ plus: 2, minus: 2 });
    expect(parseUnifiedDiffLines(`${first}\n${second}`).filter((line) => line.kind === "meta").map((line) => line.text))
      .toEqual(["--- a/one.ts", "+++ b/one.ts", "--- a/two.ts", "+++ b/two.ts"]);
  });

  it("still reads removed lines that merely start with dashes inside an open hunk", () => {
    const patch = ["@@ -1,3 +1,1 @@", "-keep", "--- not a header", "-+++ not a header either"].join("\n");
    expect(countUnifiedDiffLines(patch)).toEqual({ plus: 0, minus: 3 });
  });

  it("decodes Git-quoted header names", () => {
    expect(unquoteGitPath(String.raw`"a/quote\"name.ts"`)).toBe('a/quote"name.ts');
    expect(unquoteGitPath(String.raw`"tab\tname.ts"`)).toBe("tab\tname.ts");
    expect(unquoteGitPath(String.raw`"\344\270\255.ts"`)).toBe("中.ts");
    expect(unquoteGitPath("plain.ts")).toBe("plain.ts");
    expect(parseDiffGitHeader(String.raw`diff --git "a/quote\"name.ts" "b/quote\"name.ts"`)).toEqual({ oldPath: 'quote"name.ts', newPath: 'quote"name.ts' });
    expect(parseDiffGitHeader("diff --git a/dir/file name.ts b/dir/file name.ts")).toEqual({ oldPath: "dir/file name.ts", newPath: "dir/file name.ts" });
    expect(parseDiffGitHeader(String.raw`diff --git a/old.ts "b/new\"name.ts"`)).toEqual({ oldPath: "old.ts", newPath: 'new"name.ts' });
    const patch = [String.raw`diff --git "a/quote\"name.ts" "b/quote\"name.ts"`, String.raw`--- "a/quote\"name.ts"`, "+++ /dev/null", "@@ -1,1 +0,0 @@", "-gone"].join("\n");
    expect(unifiedDiffFilePaths(parseUnifiedDiffLines(patch))).toEqual({ oldPath: 'quote"name.ts', newPath: "/dev/null" });
  });
});
