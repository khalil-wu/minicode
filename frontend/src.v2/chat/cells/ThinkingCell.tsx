import { useEffect, useState } from "react";
import type React from "react";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import type { ThinkingCellState } from "./cellTypes";
import "./cells.css";

/**
 * ThinkingCell — Claude Code style ∴ Thinking display.
 *
 * Collapsed: "∴ Thinking" dim italic
 * Expanded (streaming or clicked): "∴ Thinking..." + indented markdown
 */
export function ThinkingCell({ cell, isStreaming = false }: { cell: ThinkingCellState; isStreaming?: boolean }) {
  // Auto-expand during streaming, auto-collapse after
  const [expanded, setExpanded] = useState(isStreaming);

  useEffect(() => {
    if (isStreaming) setExpanded(true);
  }, [isStreaming]);

  const hasContent = Boolean(cell.content?.trim());
  const preview = hasContent && !expanded
    ? cell.content.slice(0, 80).replace(/\n/g, " ").trim() + (cell.content.length > 80 ? "..." : "")
    : "";

  // Format phase for display
  const phaseLabel = cell.phase ? formatPhase(cell.phase) : "";

  return (
    <div className="thinking-cell">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="thinking-cell-header"
        aria-expanded={expanded}
      >
        <span className="thinking-cell-therefore">
          {isStreaming ? "∴ Thinking..." : "∴ Thinking"}
        </span>
        {phaseLabel && (
          <span className="thinking-cell-phase">({phaseLabel})</span>
        )}
        {!expanded && preview && (
          <span className="thinking-cell-preview">
            {" · "}{preview}
          </span>
        )}
        {!isStreaming && (
          <span className="thinking-cell-hint">
            ({expanded ? "click to collapse" : "click to expand"})
          </span>
        )}
      </button>
      {expanded && hasContent && (
        <div className="thinking-cell-content">
          <MarkdownRenderer content={cell.content} />
        </div>
      )}
    </div>
  );
}

function formatPhase(phase: string): string {
  // Convert snake_case/camelCase to readable text
  const readable = phase
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .toLowerCase();

  // Common phase translations
  const translations: Record<string, string> = {
    "analyzing requirements": "分析需求",
    "planning approach": "规划方案",
    "deciding tools": "选择工具",
    "reviewing context": "审查上下文",
    "synthesizing": "综合分析",
  };

  return translations[readable] || readable;
}
