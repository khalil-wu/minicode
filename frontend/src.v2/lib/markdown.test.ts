import { describe, expect, it } from "vitest";
import {
  createMarkdownHeadingIdAssigner,
  decodeMarkdownFragment,
  extractInlineCitationIndexes,
  markdownHeadingSlug,
} from "./markdown";

describe("Markdown helpers", () => {
  it("extracts positive inline citation indexes once", () => {
    expect([...extractInlineCitationIndexes("See [2], [1], [2], and [0].")]).toEqual([2, 1]);
  });

  it("creates deterministic heading slugs", () => {
    expect(markdownHeadingSlug("  API_Über!  ")).toBe("api-über");
    expect(markdownHeadingSlug("***")).toBe("section");
  });

  it("preserves malformed URI fragments instead of breaking Markdown rendering", () => {
    expect(decodeMarkdownFragment("hello%20world")).toBe("hello world");
    expect(decodeMarkdownFragment("broken%fragment")).toBe("broken%fragment");
  });

  it("keeps positionless duplicate heading IDs distinct and stable per render", () => {
    const headingId = createMarkdownHeadingIdAssigner("scope");
    const firstRender = [headingId("Overview"), headingId("Overview")];

    headingId.reset();

    expect(firstRender).toEqual(["scope-overview", "scope-overview-2"]);
    expect([headingId("Overview"), headingId("Overview")]).toEqual(firstRender);
  });

  it("restarts heading ordinals when positions shift between renders", () => {
    const headingId = createMarkdownHeadingIdAssigner("scope");
    expect([headingId("Overview", 8), headingId("Overview", 16)]).toEqual([
      "scope-overview",
      "scope-overview-2",
    ]);

    headingId.reset();

    expect([headingId("Overview", 2), headingId("Overview", 10)]).toEqual([
      "scope-overview",
      "scope-overview-2",
    ]);
  });
});
