import { memo, useEffect, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, Sparkles } from "lucide-react";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import type { ThinkingCellState } from "./cellTypes";
import "./cells.css";

export const ThinkingCell = memo(function ThinkingCell({ cell, isStreaming = false }: { cell: ThinkingCellState; isStreaming?: boolean }) {
  const [expanded, setExpanded] = useState(isStreaming);

  useEffect(() => {
    setExpanded(isStreaming);
  }, [isStreaming]);

  const hasContent = Boolean(cell.content?.trim());
  const renderedContent = cell.content;
  const renderAsProcessText = hasContent && (
    cell.source === "commentary" ||
    cell.source === "model_preamble" ||
    cell.source === "post_tool" ||
    cell.source === "runtime"
  );
  if (renderAsProcessText) {
    return (
      <div className="thinking-cell thinking-cell-process" data-source={cell.source}>
        <div className="thinking-cell-process-content">
          <MarkdownRenderer content={renderedContent} isStreaming={isStreaming} />
        </div>
      </div>
    );
  }

  const label = labelForSource(cell.source, isStreaming);

  return (
    <div className="thinking-cell" data-streaming={isStreaming ? "true" : "false"}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="thinking-cell-header"
        aria-expanded={expanded}
      >
        <Sparkles size={14} className="thinking-cell-icon" />
        <span className="thinking-cell-therefore">{label}</span>
        <span className="thinking-cell-toggle">
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>
      {expanded && hasContent && (
        <div className="thinking-cell-content-wrap" data-open={expanded ? "true" : "false"}>
          <div className="thinking-cell-content">
            <MarkdownRenderer content={renderedContent} isStreaming={isStreaming} />
          </div>
        </div>
      )}
    </div>
  );
});

function labelForSource(source: ThinkingCellState["source"], isStreaming: boolean): string {
  if (source === "commentary") return "";
  if (source === "model_preamble") return "";
  if (source === "post_tool") return "";
  if (source === "runtime") return "运行状态";
  if (source === "provider" || source === "reasoning") {
    return isStreaming ? "Thinking..." : "Thinking";
  }
  return isStreaming ? "Thinking..." : "Thinking";
}
