import { useState } from "react";
import { Check, CheckCircle2, Plus, Sparkles, X } from "lucide-react";
import { useAppStore } from "../../stores";
import { createTodoItem } from "../../lib/todo-utils";
import "./task-suggestions.css";

/**
 * A suggestion is data owned by the agent/backend.  The renderer deliberately
 * does not derive suggestions from prompt text or manufacture confidence
 * values; if no authoritative suggestions are supplied, nothing is shown.
 */
export interface TaskSuggestion {
  id: string;
  content: string;
  reason?: string;
  confidence?: number;
}

interface TaskSuggestionsProps {
  suggestions?: readonly TaskSuggestion[];
  onDismiss?: () => void;
}

export function TaskSuggestions({ suggestions = [], onDismiss }: TaskSuggestionsProps) {
  const [adopted, setAdopted] = useState<Set<string>>(new Set());
  const addTodo = useAppStore((s) => s.addTodo);

  const adoptSuggestion = (suggestion: TaskSuggestion) => {
    addTodo(createTodoItem(suggestion.content));
    setAdopted((prev) => new Set(prev).add(suggestion.id));
  };

  const adoptAll = () => {
    suggestions.forEach((suggestion) => {
      if (!adopted.has(suggestion.id)) adoptSuggestion(suggestion);
    });
  };

  if (suggestions.length === 0) return null;

  const allAdopted = suggestions.every((suggestion) => adopted.has(suggestion.id));

  return (
    <div className="task-suggestions">
      <div className="task-suggestions-header">
        <Sparkles size={16} className="task-suggestions-icon" />
        <span>建议的任务步骤</span>
        {!allAdopted && (
          <button
            type="button"
            className="task-suggestions-adopt-all"
            onClick={adoptAll}
          >
            全部采纳
          </button>
        )}
        <button
          type="button"
          className="task-suggestions-close mc-icon-button mc-icon-button-compact"
          onClick={onDismiss}
          title="关闭建议"
          aria-label="关闭建议"
        >
          <X size={14} />
        </button>
      </div>

      <div className="task-suggestions-list">
        {suggestions.map((suggestion) => {
          const isAdopted = adopted.has(suggestion.id);
          const confidence = typeof suggestion.confidence === "number"
            ? Math.round(suggestion.confidence * 100)
            : undefined;
          return (
            <div
              key={suggestion.id}
              className={`task-suggestion-item ${isAdopted ? "adopted" : ""}`}
            >
              <div className="task-suggestion-content">
                <div className="task-suggestion-text">{suggestion.content}</div>
                {(suggestion.reason || confidence != null) && (
                  <div className="task-suggestion-meta">
                    {suggestion.reason && (
                      <span className="task-suggestion-reason">{suggestion.reason}</span>
                    )}
                    {confidence != null && (
                      <span className="task-suggestion-confidence">{confidence}% 置信度</span>
                    )}
                  </div>
                )}
              </div>
              <button
                type="button"
                className="task-suggestion-adopt mc-icon-button mc-icon-button-accent"
                onClick={() => adoptSuggestion(suggestion)}
                disabled={isAdopted}
                title={isAdopted ? "已采纳" : "采纳建议"}
                aria-label={isAdopted ? `已采纳：${suggestion.content}` : `采纳建议：${suggestion.content}`}
              >
                {isAdopted ? <Check size={16} /> : <Plus size={16} />}
              </button>
            </div>
          );
        })}
      </div>

      {allAdopted && (
        <div className="task-suggestions-footer">
          <CheckCircle2 size={16} strokeWidth={1.75} aria-hidden="true" />
          <span>所有建议已采纳！</span>
        </div>
      )}
    </div>
  );
}
