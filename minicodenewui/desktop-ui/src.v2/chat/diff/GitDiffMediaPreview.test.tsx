/* @vitest-environment jsdom */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { diffPreviewKind, GitDiffMediaPreview } from "./GitDiffMediaPreview";
import type { GitDiffContentResult } from "../../protocol/workspace-types";

vi.mock("../messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <article>{content}</article> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("uses Source's actual kind gates in all five comparison scopes", () => {
  for (const source of ["branch", "commit", "uncommitted", "unstaged", "staged"] as const) {
    expect(diffPreviewKind("image.PNG", false, false, source)).toBe("image");
    expect(diffPreviewKind("report.pdf", true, false, source)).toBe("pdf");
    expect(diffPreviewKind("diagram.svg", false, false, source)).toBe("diff");
    expect(diffPreviewKind("diagram.svg", false, true, source)).toBe("image");
    expect(diffPreviewKind("README.md", false, true, source)).toBe(source === "staged" ? "diff" : "markdown");
    expect(diffPreviewKind("README.md", true, true, source)).toBe("diff");
    expect(diffPreviewKind("README.md", false, false, source)).toBe("diff");
  }
});

it("renders independently fetched binary sides and retires their exact blob URLs", async () => {
  const create = vi.fn((blob: Blob) => `blob:owned-${blob.size}-${create.mock.calls.length}`);
  const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  const rows: GitDiffContentResult[] = [
    { type: "success", path: "image.png", side: "old", source_kind: "object", data_base64: btoa("old"), media_type: "image/png" },
    { type: "success", path: "image.png", side: "new", source_kind: "disk", data_base64: btoa("current"), media_type: "image/png" },
  ];
  const view = render(<GitDiffMediaPreview path="image.png" kind="image" loading={false} results={rows} status="changed" workspaceRoot="C:/owned" conversationId="original" />);
  await waitFor(() => expect(view.container.querySelectorAll("img")).toHaveLength(2));
  expect(create.mock.calls.map(([blob]) => blob.size)).toEqual([3, 7]);
  const urls = [...view.container.querySelectorAll("img")].map(image => image.src);
  view.unmount();
  expect(revoke.mock.calls.map(([url]) => url).sort()).toEqual(urls.sort());
});

it("uses the supplied current-path Markdown text without inventing a historical object version", () => {
  render(<GitDiffMediaPreview path="README.md" kind="markdown" loading={false} status="changed" workspaceRoot="C:/owned" conversationId="original"
    results={[{ type: "success", path: "README.md", side: "new", source_kind: "disk", lines: ["current path\r\n", "text"] }]} />);
  expect(screen.getByRole("article").textContent).toBe("current path\r\ntext");
});
