/**
 * Editor-side placement for inline agent-edit review.
 *
 * Pure helpers over AgentEditBlock so the EditorPanel hook stays thin and the
 * placement logic is unit-tested without a live Monaco instance. Line numbers
 * are 1-based to match Monaco ranges.
 */
import type { AgentEditBlock } from "./agent-edit-review";

export type AgentEditDecorationKind = "add" | "delete" | "replace";

export interface AgentEditDecoration {
  blockKey: string;
  kind: AgentEditDecorationKind;
  /** 1-based inclusive line range of the added text in the current buffer. */
  startLine: number;
  endLine: number;
}

const blockKind = (block: AgentEditBlock): AgentEditDecorationKind => {
  if (block.added.length === 0) return "delete";
  if (block.removed.length === 0) return "add";
  return "replace";
};

/** One decoration per block, covering its added lines (or the anchor line for a pure deletion). */
export function buildAgentEditDecorations(blocks: AgentEditBlock[]): AgentEditDecoration[] {
  return blocks.map((block) => {
    const kind = blockKind(block);
    const startLine = Math.max(1, block.line);
    const endLine = block.added.length > 0 ? startLine + block.added.length - 1 : startLine;
    return { blockKey: block.key, kind, startLine, endLine };
  });
}

/** Anchor line used to reveal a block in the viewport. */
export function blockAnchorLine(block: AgentEditBlock): number {
  return Math.max(1, block.line);
}

/**
 * Index of the block to treat as "current" for a cursor on `cursorLine`:
 * the last block that starts at or before the cursor, else the first block.
 */
export function currentAgentEditBlockIndex(blocks: AgentEditBlock[], cursorLine: number): number {
  if (blocks.length === 0) return -1;
  let index = 0;
  for (let candidate = 0; candidate < blocks.length; candidate += 1) {
    if (blockAnchorLine(blocks[candidate]) <= cursorLine) index = candidate;
    else break;
  }
  return index;
}

/** Next block index after the cursor, wrapping to the first. */
export function nextAgentEditBlockIndex(blocks: AgentEditBlock[], cursorLine: number): number {
  if (blocks.length === 0) return -1;
  for (let index = 0; index < blocks.length; index += 1) {
    if (blockAnchorLine(blocks[index]) > cursorLine) return index;
  }
  return 0;
}

/** Previous block index before the cursor, wrapping to the last. */
export function previousAgentEditBlockIndex(blocks: AgentEditBlock[], cursorLine: number): number {
  if (blocks.length === 0) return -1;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    if (blockAnchorLine(blocks[index]) < cursorLine) return index;
  }
  return blocks.length - 1;
}
