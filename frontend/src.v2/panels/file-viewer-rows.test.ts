import { expect, it } from "vitest";
import { fileViewerRows } from "./file-viewer-rows";

it("retains complete saved contents around patch hunks with real old and new line numbers", () => {
  const rows = fileViewerRows("one\r\nnew\r\nthree\r\nfour\r\n", "--- a/a.ts\n+++ b/a.ts\n@@ -2 +2 @@\n-old\n+new\n", true);
  expect(rows).toEqual([
    { text: "one", kind: "context", oldLine: 1, newLine: 1 }, { text: "old", kind: "del", oldLine: 2 },
    { text: "new", kind: "add", newLine: 2 }, { text: "three", kind: "context", oldLine: 3, newLine: 3 },
    { text: "four", kind: "context", oldLine: 4, newLine: 4 },
  ]);
});

it("renders a complete deletion whose new hunk starts at zero", () => {
  expect(fileViewerRows("", "--- a/a.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-deleted\n\\ No newline at end of file\n", true))
    .toEqual([{ text: "deleted", kind: "del", oldLine: 1 }]);
});
