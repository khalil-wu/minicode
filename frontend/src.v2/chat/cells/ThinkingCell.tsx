import { useEffect, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, Sparkles } from "lucide-react";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import type { ThinkingCellState } from "./cellTypes";
import "./cells.css";

function normalizeProviderReasoningSummary(content: string, cell: ThinkingCellState): string {
  if (cell.source !== "provider" && cell.source !== "reasoning") return content;
  if (cell.providerReasoningType !== "reasoning_summary_text") return content;
  return content.replace(/\*\*([^*\n]+?)\*\*(\n{2,})/g, "$1$2");
}

export function ThinkingCell({ cell, isStreaming = false }: { cell: ThinkingCellState; isStreaming?: boolean }) {
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!isStreaming) setExpanded(false);
  }, [isStreaming]);

  const hasContent = Boolean(cell.content?.trim());
  const renderedContent = hasContent ? normalizeProviderReasoningSummary(cell.content, cell) : cell.content;
  const renderAsProcessText = hasContent && (
    cell.source === "model_preamble" ||
    cell.source === "post_tool" ||
    cell.source === "provider" ||
    cell.source === "runtime" ||
    cell.source === "reasoning"
  );
  if (renderAsProcessText) {
    return (
      <div className="thinking-cell thinking-cell-process" data-source={cell.source}>
        <div className="thinking-cell-process-content">
          <MarkdownRenderer content={renderedContent} />
        </div>
      </div>
    );
  }

  const preview = hasContent && !expanded
    ? compactPreview(cell.content, isStreaming ? 110 : 96)
    : "";

  const phaseLabel = cell.phase ? formatPhase(cell.phase) : "";
  const label = labelForSource(cell.source, isStreaming);
  const charCount = hasContent ? cell.content.trim().length : 0;

  return (
    <div className="thinking-cell" data-streaming={isStreaming ? "true" : "false"}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="thinking-cell-header"
        aria-expanded={expanded}
      >
        <Sparkles size={12} className="thinking-cell-icon" />
        <span className="thinking-cell-therefore">
          {charCount ? `${label} · ${charCount} 字` : label}
        </span>
        {phaseLabel && (
          <span className="thinking-cell-phase">({phaseLabel})</span>
        )}
        {!expanded && preview && (
          <span className="thinking-cell-preview">
            {" · "}{preview}
          </span>
        )}
        <span className="thinking-cell-toggle">
          {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        </span>
      </button>
      {hasContent && (
        <div className="thinking-cell-content-wrap" data-open={expanded ? "true" : "false"}>
          <div className="thinking-cell-content">
            <MarkdownRenderer content={renderedContent} />
          </div>
        </div>
      )}
    </div>
  );
}

function labelForSource(source: ThinkingCellState["source"], isStreaming: boolean): string {
  if (source === "model_preamble") return "";
  if (source === "post_tool") return "";
  if (source === "runtime") return "运行状态";
  if (source === "provider") return "";
  return "";
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
