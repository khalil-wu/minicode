import { describe, expect, it } from "vitest";
import { hydrateMessages } from "./transcriptHydration";
import { projectMessagesToTurns } from "./chatSurfaceState";
import { buildOutput } from "../shell/activitySidebarState";
import type { ChatMessage } from "../stores/types";

describe("Source question and output projection", () => {
  it("restores an async question outside collapsed process and keeps the final answer independent", () => {
    const messages = hydrateMessages([{ id: "assistant", role: "assistant", content: "Finished the independent work.",
      timestamp: 1, blocks: [
        { type: "text", itemId: "async:question", source: "async", delivery: "async", status: "completed", content: "Which module?", questions: [{ title: "Which module?", options: ["A", "B"] }] },
        { type: "text", itemId: "final", source: "model_final", status: "completed", content: "Finished the independent work." },
      ] }]);
    const turn = projectMessagesToTurns(messages, false)[0];
    expect(turn.asyncMessages?.[0].questions).toEqual([{ title: "Which module?", options: ["A", "B"] }]);
    expect(turn.finalAnswerCell?.markdownSource).toBe("Finished the independent work.");
    expect(turn.committedCells.some(cell => cell.kind === "thinking" && cell.content.includes("Which module?"))).toBe(false);
  });

  it("keeps real files and tool evidence while excluding oversized execution caches from delivery lists", () => {
    const message: ChatMessage = { id: "answer", role: "assistant", timestamp: 1, content: "", artifacts: [
      { artifactId: "cache", kind: "text", summary: "Code execution output" },
    ], blocks: [{ type: "tool_call", record: { id: "execute", name: "tool_exec", args: {}, status: "success", startedAt: 1,
      artifactId: "cache", artifactKind: "text", artifactMediaType: "text/plain", resultFull: "Original tool evidence",
      outputFiles: [{ path: "output/report.md", size: 30 }] } }] };
    const output = buildOutput([message], null);
    expect(output.map(item => item.path)).toEqual(["output/report.md"]);
    expect(output.some(item => item.artifactId === "cache")).toBe(false);
    expect(message.blocks?.[0].type === "tool_call" && message.blocks[0].record.resultFull).toBe("Original tool evidence");
  });
});
