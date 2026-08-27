import { memo } from "react";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { readableToolLabel } from "../toolDisplayName";
import type { ThinkingCellState } from "./cellTypes";
import "./cells.css";

export const ThinkingCell = memo(function ThinkingCell({
  cell,
  isStreaming = false,
}: {
  cell: ThinkingCellState;
  isStreaming?: boolean;
}) {
  const streaming = Boolean(isStreaming || cell.isStreaming);
  const content = readableToolLabel(cell.content).trim();
  if (!content) return null;
  const providerReasoning = ["provider", "reasoning"].includes(cell.source);
  // Provider reasoning is ephemeral. Model commentary is durable process
  // narration: it stays in sequence, has no label/rail/card, and closes the
  // preceding tool segment as soon as it arrives.
  if (providerReasoning && !streaming) return null;
  return (
    <div
      className={`thinking-cell ${providerReasoning ? "thinking-cell-live" : "thinking-cell-commentary"}`}
      data-source={cell.source}
      data-streaming={streaming ? "true" : "false"}
    >
      <MarkdownRenderer content={content} isStreaming={streaming} />
    </div>
  );
});
