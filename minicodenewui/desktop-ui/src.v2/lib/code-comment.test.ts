import { describe, expect, it } from "vitest";
import { parseCodeComments, withoutCodeCommentDirectives } from "./code-comment";

describe("installed desktop code-comment contract", () => {
  it("reads required attributes, escaped quotes, Windows owner paths and priority once", () => {
    const raw = String.raw`::code-comment{title="Wrong value" body="Use \"value\" here } carefully." file="C:\work\src\app.py" start=3 end=5 priority="2"}`;
    expect(parseCodeComments(raw, "C:/work")).toEqual([{ file: "src/app.py", title: "[P2] Wrong value", body: 'Use "value" here } carefully.', start: 3, end: 5, priority: 2 }]);
  });
  it("uses the shortest normalized right-side range and honors existing priority labels", () => {
    expect(parseCodeComments('::code-comment{title="[P1] Fix" body="Reason" file="work/src/a.ts" start=0 end=-2 priority=2}', "C:/work"))
      .toEqual([{ file: "src/a.ts", title: "[P1] Fix", body: "Reason", start: 1, end: 1, priority: 2 }]);
  });
  it("does not turn incomplete attributes or prose into findings", () => {
    expect(parseCodeComments('Text ::code-comment{title="Inline" body="Reason" file="x"}\n::code-comment{title="Incomplete" file="x"}', "C:/work")).toEqual([]);
  });
  it("does not turn a fenced directive example into a finding", () => {
    const directive = '::code-comment{title="Fix" body="Reason" file="x"}';
    expect(parseCodeComments("```text\n" + directive + "\n```\n~~~\n" + directive + "\n~~~", "C:/work")).toEqual([]);
    expect(parseCodeComments("```text\n" + directive + "\n```\n" + directive, "C:/work")).toHaveLength(1);
  });
  it("keeps fenced examples in visible markdown while consuming real directives", () => {
    const directive = '::code-comment{title="Fix" body="Reason" file="x"}';
    expect(withoutCodeCommentDirectives(`Visible request\n${directive}\n\`\`\`text\n${directive}\n\`\`\``))
      .toBe(`Visible request\n\`\`\`text\n${directive}\n\`\`\``);
  });
});
