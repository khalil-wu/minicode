import { describe, expect, it } from "vitest";
import { createChangedFileMatcher, rankChangedFilePaths } from "./changes-file-matcher";

describe("Source Changes file matcher", () => {
  it("uses filename then parent ordering only for an empty query, without changing its input", () => {
    const paths = ["tests/a.ts", "src/b.ts", "src/a.ts", "z.ts"];
    expect(rankChangedFilePaths(paths, " \t\n")).toEqual(["src/a.ts", "tests/a.ts", "src/b.ts", "z.ts"]);
    expect(paths).toEqual(["tests/a.ts", "src/b.ts", "src/a.ts", "z.ts"]);
    expect(createChangedFileMatcher(" ")("anything.ts")).toBe(0);
  });

  it("keeps the Source prefix, boundary, separator and case scoring constants", () => {
    // Traced from matchingDegree + boostStartMatch + SWc, without evaluating installed JS.
    const score = createChangedFileMatcher("a");
    expect(["a", "ab", "A", "ba", "bA", "b_a", "z/a", "none"].map(score))
      .toEqual([110039, 110028, 110019, 18, 8, 10027, 10007, 0]);
  });

  it("scores the filename first and preserves original equal-score order", () => {
    const paths = ["b/same.ts", "a/same.ts", "x/unrelated.ts", "c/same.ts"];
    expect(rankChangedFilePaths(paths, " same ")).toEqual(["b/same.ts", "a/same.ts", "c/same.ts"]);
    expect(rankChangedFilePaths(paths, "")).toEqual(["a/same.ts", "b/same.ts", "c/same.ts", "x/unrelated.ts"]);
    expect(rankChangedFilePaths(["z/ab", "ba", "a"], "a")).toEqual(["a", "z/ab", "ba"]);
  });

  it("matches camel humps and word fragments without skipping arbitrary lower-case text or digits", () => {
    const camel = createChangedFileMatcher("FB");
    expect(camel("FooBar.ts")).toBeGreaterThan(0);
    expect(camel("fizzbuzz.ts")).toBe(0);
    expect(createChangedFileMatcher("foo bar")("FooBar.ts")).toBeGreaterThan(0);
    expect(createChangedFileMatcher("12")("102.ts")).toBe(0);
    expect(createChangedFileMatcher("12")("12.ts")).toBeGreaterThan(0);
  });

  it("uses the Source path separator normalization and basename fallback", () => {
    const slash = createChangedFileMatcher("src/file"), backslash = createChangedFileMatcher("src\\file");
    expect(slash("src/file.ts")).toBeGreaterThan(0);
    expect(slash("src/file.ts")).toBe(slash("src\\file.ts"));
    expect(backslash("src/file.ts")).toBe(slash("src/file.ts"));
    expect(slash("file.ts")).toBeGreaterThan(0);
    expect(slash("unrelated.ts")).toBe(0);
  });

  it("preserves case conversions that would change a Unicode character's UTF-16 length", () => {
    const score = createChangedFileMatcher("İ");
    expect(score("İ.ts")).toBeGreaterThan(0);
    expect(score("i.ts")).toBe(0);
  });

  it("retains the Source long-pattern substring behavior and real duplicate entries", () => {
    const pattern = "a".repeat(100), score = createChangedFileMatcher(pattern);
    expect(score(`prefix${pattern}.ts`)).toBeGreaterThan(0);
    expect(score("a".repeat(99))).toBe(0);
    expect(rankChangedFilePaths(["x/a.ts", "x/a.ts", "b.ts"], "a.ts")).toEqual(["x/a.ts", "x/a.ts"]);
  });
});
