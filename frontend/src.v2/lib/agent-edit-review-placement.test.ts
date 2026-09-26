import { describe, expect, it } from "vitest";
import type { AgentEditBlock } from "./agent-edit-review";
import {
  buildAgentEditDecorations,
  blockAnchorLine,
  currentAgentEditBlockIndex,
  nextAgentEditBlockIndex,
  previousAgentEditBlockIndex,
} from "./agent-edit-review-placement";

const block = (key: string, line: number, added: string[], removed: string[]): AgentEditBlock => ({ key, line, added, removed });

const BLOCKS: AgentEditBlock[] = [
  block("a", 3, ["LINE3"], ["line3"]),   // replace
  block("b", 16, ["inserted"], []),       // add
  block("c", 19, [], ["line18"]),         // delete
];

describe("agent edit review placement", () => {
  it("classifies each block and covers its added lines", () => {
    expect(buildAgentEditDecorations(BLOCKS)).toEqual([
      { blockKey: "a", kind: "replace", startLine: 3, endLine: 3 },
      { blockKey: "b", kind: "add", startLine: 16, endLine: 16 },
      { blockKey: "c", kind: "delete", startLine: 19, endLine: 19 },
    ]);
  });

  it("covers multi-line additions", () => {
    const [decoration] = buildAgentEditDecorations([block("m", 10, ["x", "y", "z"], [])]);
    expect(decoration).toEqual({ blockKey: "m", kind: "add", startLine: 10, endLine: 12 });
  });

  it("anchors on the block line", () => {
    expect(BLOCKS.map(blockAnchorLine)).toEqual([3, 16, 19]);
  });

  it("finds the current block for a cursor line", () => {
    expect(currentAgentEditBlockIndex(BLOCKS, 1)).toBe(0);
    expect(currentAgentEditBlockIndex(BLOCKS, 3)).toBe(0);
    expect(currentAgentEditBlockIndex(BLOCKS, 17)).toBe(1);
    expect(currentAgentEditBlockIndex(BLOCKS, 999)).toBe(2);
    expect(currentAgentEditBlockIndex([], 5)).toBe(-1);
  });

  it("navigates to the next block, wrapping to the first", () => {
    expect(nextAgentEditBlockIndex(BLOCKS, 1)).toBe(0);
    expect(nextAgentEditBlockIndex(BLOCKS, 3)).toBe(1);
    expect(nextAgentEditBlockIndex(BLOCKS, 16)).toBe(2);
    expect(nextAgentEditBlockIndex(BLOCKS, 19)).toBe(0);
  });

  it("navigates to the previous block, wrapping to the last", () => {
    expect(previousAgentEditBlockIndex(BLOCKS, 20)).toBe(2);
    expect(previousAgentEditBlockIndex(BLOCKS, 19)).toBe(1);
    expect(previousAgentEditBlockIndex(BLOCKS, 3)).toBe(2);
    expect(previousAgentEditBlockIndex(BLOCKS, 1)).toBe(2);
  });
});
