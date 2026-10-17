import { createTwoFilesPatch } from "diff";
import { parser as javascript } from "@lezer/javascript";
import { parser as python } from "@lezer/python";
import { parser as rust } from "@lezer/rust";
import { parser as go } from "@lezer/go";
import { parser as java } from "@lezer/java";
import { describe, expect, it } from "vitest";
import { buildFullDiffContext } from "./full-content-metadata";
import { diffPresentationLanguage, filterImportOnlyDiffHunks } from "./file-diff-presentation";

const js = javascript.configure({ dialect: "jsx" });
const lines = (value: string) => value.match(/[^\n]*\n|[^\n]+$/g) ?? [];
const context = (oldValue: string, newValue: string) => buildFullDiffContext(
  createTwoFilesPatch("a/file", "b/file", oldValue, newValue, "", "", { context: 0 }), lines(oldValue), lines(newValue),
)!;

describe("frozen Source top-level import-only hunk presentation", () => {
  it.each([
    ["a.ts", "typescript"], ["a.mts", "typescript"], ["a.cts", "typescript"], ["a.tsx", "tsx"],
    ["a.jsx", "javascript"], ["a.mjs", "javascript"], ["a.cjs", "javascript"], ["a.pyi", "python"],
    ["a.rs", "rust"], ["a.go", "go"], ["a.java", "java"], ["a.rb", undefined],
  ])("selects the actual Source extension dialect %s", (path, language) => expect(diffPresentationLanguage(path!)).toBe(language));

  it.each([
    ["a.js", js, "import A from 'a';\n", "import B from 'b';\n"],
    ["a.ts", javascript.configure({ dialect: "ts" }), "import type { A } from 'a';\n", "import type { B } from 'b';\n"],
    ["a.tsx", javascript.configure({ dialect: "ts jsx" }), "import A from 'a';\n", "import B from 'b';\n"],
    ["a.py", python, "from a import (\n A,\n)\n", "from b import (\n B,\n)\n"],
    ["a.rs", rust, "use a::{\n A,\n};\n", "use b::{\n B,\n};\n"],
    ["a.go", go, 'import (\n "a"\n)\n', 'import (\n "b"\n)\n'],
    ["a.java", java, "import static a.A;\n", "import static b.B;\n"],
  ] as const)("uses real language grammar for %s and multiline imports", (path, parser, before, after) => {
    const original = context(before, after), result = filterImportOnlyDiffHunks(original, path, parser);
    expect(result.hunks).toEqual([]);
    expect(result.isPartial).toBe(true);
    expect(original.hunks).toHaveLength(1);
  });

  it("retains comments on either real file side, and preserves a mixed hunk entirely", () => {
    const only = context("import A from 'a';\n", "import B from 'b';\n");
    expect(filterImportOnlyDiffHunks(only, "a.js", js, [{ side: "additions", lineNumber: 1 }])).toBe(only);
    expect(filterImportOnlyDiffHunks(only, "a.js", js, [{ side: "deletions", lineNumber: 1 }])).toBe(only);
    const before = "import A from 'a';\nconst value = 1;\n", after = "import B from 'b';\nconst value = 2;\n";
    const mixed = buildFullDiffContext(createTwoFilesPatch("a/x", "b/x", before, after), lines(before), lines(after))!;
    expect(filterImportOnlyDiffHunks(mixed, "a.js", js)).toBe(mixed);
  });

  it("keeps original file/patch coordinates while rebuilding only retained hunk bytes", () => {
    const original = context("import A from 'a';\n\n// same\nconst value = 1;\n", "import B from 'b';\n\n// same\nconst value = 2;\n");
    const result = filterImportOnlyDiffHunks(original, "a.js", js);
    expect(result.hunks).toHaveLength(1);
    expect(result.hunks[0]).toMatchObject({ index: 1, collapsedBefore: 3, splitLineStart: 3, unifiedLineStart: 3 });
    expect(result.hunks[0].body.map(line => [line.lineIndex, line.unifiedIndex, line.oldLine, line.newLine])).toEqual([[3, 3, 4, undefined], [4, 4, undefined, 4]]);
    expect(result.hunks[0].body.map(line => line.lineIndex)).toEqual(original.hunks[1].body.map(line => line.lineIndex));
    expect(result.oldLines).toEqual(["const value = 1;\n"]);
    expect(result.newLines).toEqual(["const value = 2;\n"]);
    expect(result.hunks[0].before).toEqual([]);
    expect(result.trailing).toEqual([]);
  });

  it.each([
    ["function f() {\n import A from 'a';\n}\n", "function f() {\n import B from 'b';\n}\n"],
    ["import A from 'a'; runA();\n", "import B from 'b'; runB();\n"],
    ["/* note */ import A from 'a';\n", "/* note */ import B from 'b';\n"],
    ["export { A } from 'a';\n", "export { B } from 'b';\n"],
    ["import { A from 'a';\n", "import { B from 'b';\n"],
  ])("does not hide nested, mixed-line, export or malformed syntax", (before, after) => {
    const original = context(before, after);
    expect(filterImportOnlyDiffHunks(original, "a.js", js)).toBe(original);
  });

  it("permits same-line trailing comments and excludes Rust public API uses", () => {
    expect(filterImportOnlyDiffHunks(context("import A from 'a'; // keep\n", "import B from 'b'; // keep\n"), "a.js", js).hunks).toEqual([]);
    const exported = context("pub use a::A;\n", "pub use b::B;\n");
    expect(filterImportOnlyDiffHunks(exported, "a.rs", rust)).toBe(exported);
  });

  it("leaves partial, unsupported, parser-unavailable, and new/deleted presentations unchanged", () => {
    const original = context("import A from 'a';\n", "import B from 'b';\n");
    for (const fileType of ["new", "deleted"] as const) {
      const special = { ...original, fileType };
      expect(filterImportOnlyDiffHunks(special, "a.js", js)).toBe(special);
    }
    const partial = { ...original, isPartial: true };
    expect(filterImportOnlyDiffHunks(partial, "a.js", js)).toBe(partial);
    expect(filterImportOnlyDiffHunks(original, "a.rb", js)).toBe(original);
    expect(filterImportOnlyDiffHunks(original, "a.js", undefined)).toBe(original);
  });
});
