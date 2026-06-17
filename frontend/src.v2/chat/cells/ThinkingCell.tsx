import { useEffect, useState } from "react";
import type React from "react";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import type { ThinkingCellState } from "./cellTypes";
import "./cells.css";

export function ThinkingCell({ cell, isStreaming = false }: { cell: ThinkingCellState; isStreaming?: boolean }) {
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!isStreaming) setExpanded(false);
  }, [isStreaming]);

  const hasContent = Boolean(cell.content?.trim());
  const renderAsProcessText = hasContent && (cell.source === "model_preamble" || cell.source === "runtime" || cell.source === "reasoning");
  if (renderAsProcessText) {
    return (
      <div className="thinking-cell thinking-cell-process" data-source={cell.source}>
        <div className="thinking-cell-process-content">
          <MarkdownRenderer content={cell.content} />
        </div>
      </div>
    );
  }

  const preview = hasContent && !expanded
    ? compactPreview(cell.content, isStreaming ? 110 : 96)
    : "";

  const phaseLabel = cell.phase ? formatPhase(cell.phase) : "";
  const label = labelForSource(cell.source, isStreaming);

  return (
    <div className="thinking-cell">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="thinking-cell-header"
        aria-expanded={expanded}
      >
        <span className="thinking-cell-therefore">
          {label}
        </span>
        {phaseLabel && (
          <span className="thinking-cell-phase">({phaseLabel})</span>
        )}
        {!expanded && preview && (
          <span className="thinking-cell-preview">
            {" · "}{preview}
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

function labelForSource(source: ThinkingCellState["source"], isStreaming: boolean): string {
  if (source === "model_preamble") return "过程";
  if (source === "runtime") return "正在处理";
  if (source === "provider") return isStreaming ? "正在思考" : "思考过程";
  return isStreaming ? "正在思考" : "思考过程";
}

function compactPreview(value: string, max = 96): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1).trimEnd()}...`;
}

function formatPhase(phase: string): string {
  const readable = phase
    .replace(/_/g, " ")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .toLowerCase();

  const translations: Record<string, string> = {
    "analyzing requirements": "分析需求",
    "planning approach": "规划方案",
    "deciding tools": "选择工具",
    "reviewing context": "审查上下文",
    "synthesizing": "综合分析",
  };

  return translations[readable] || readable;
}
