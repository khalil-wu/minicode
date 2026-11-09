/* @vitest-environment jsdom */

import { beforeEach, describe, expect, it } from "vitest";
import {
  PROMPT_HISTORY_LIMIT,
  appendPromptHistory,
  clearPromptHistory,
  promptHistoryWorkspaceKey,
  readPromptHistory,
} from "./prompt-history";

describe("prompt history", () => {
  beforeEach(() => localStorage.clear());

  it("shares history between a workspace and its managed worktree", () => {
    appendPromptHistory("C:\\Desktop\\MiniCode", "inspect the parser");
    expect(readPromptHistory("C:\\Desktop\\MiniCode\\.minicode\\worktrees\\conv_abc123"))
      .toEqual(["inspect the parser"]);
  });

  it("deduplicates newest prompts, bounds storage, and clears per workspace", () => {
    for (let index = 0; index < PROMPT_HISTORY_LIMIT + 5; index += 1) {
      appendPromptHistory("C:\\Repo", `prompt ${index}`);
    }
    appendPromptHistory("C:\\Repo", "prompt 10");
    expect(readPromptHistory("C:\\Repo")).toHaveLength(PROMPT_HISTORY_LIMIT);
    expect(readPromptHistory("C:\\Repo")[0]).toBe("prompt 10");
    appendPromptHistory("D:\\Other", "other");
    clearPromptHistory("C:\\Repo");
    expect(readPromptHistory("C:\\Repo")).toEqual([]);
    expect(readPromptHistory("D:\\Other")).toEqual(["other"]);
  });

  it("normalizes Windows workspace keys case-insensitively", () => {
    expect(promptHistoryWorkspaceKey("C:\\Desktop\\MiniCode"))
      .toBe(promptHistoryWorkspaceKey("c:/desktop/minicode/"));
  });
});
