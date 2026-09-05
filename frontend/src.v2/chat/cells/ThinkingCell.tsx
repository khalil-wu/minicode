import { memo } from "react";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { readableToolLabel } from "../toolDisplayName";
import type { ThinkingCellState } from "./cellTypes";
import {
  isProviderReasoningSummary,
  isTransientProviderReasoning,
} from "../../lib/provider-reasoning";
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
  const summary = isProviderReasoningSummary(cell);
  const transient = isTransientProviderReasoning(cell);
  if (transient && !streaming) return null;
  const variant = summary ? "summary" : transient ? "live" : "commentary";
  return (
    <div
      className={`thinking-cell md-prose thinking-cell-${variant}`}
      data-source={cell.source}
      data-reasoning-type={cell.providerReasoningType}
      data-streaming={streaming ? "true" : "false"}
    >
      <MarkdownRenderer content={content} isStreaming={streaming} />
    </div>
  );
});
