import { useState, useEffect } from "react";
import { Sparkles, X, Check } from "lucide-react";
import { useAppStore } from "../../stores";
import type { TodoItem } from "../../stores/types";
import "./task-suggestions.css";

interface TaskSuggestion {
  id: string;
  content: string;
  reason: string;
  confidence: number;
}

interface TaskSuggestionsProps {
  userPrompt: string;
  onDismiss?: () => void;
}

/**
 * AI-powered task suggestions based on user prompt
 * Analyzes the request and suggests optimal task breakdown
 */
export function TaskSuggestions({ userPrompt, onDismiss }: TaskSuggestionsProps) {
  const [suggestions, setSuggestions] = useState<TaskSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const [adopted, setAdopted] = useState<Set<string>>(new Set());
  const addTodo = useAppStore((s) => s.addTodo);

  // Generate suggestions based on prompt
  useEffect(() => {
    if (!userPrompt || userPrompt.length < 10) return;

    setLoading(true);
    // Simulate AI analysis (in real implementation, call backend API)
    setTimeout(() => {
      const generated = generateSuggestions(userPrompt);
      setSuggestions(generated);
      setLoading(false);
    }, 800);
  }, [userPrompt]);

  const adoptSuggestion = (suggestion: TaskSuggestion) => {
    const todo: TodoItem = {
      id: `todo-${Date.now()}-${suggestion.id}`,
      content: suggestion.content,
      status: "pending",
    };
    addTodo(todo);
    setAdopted((prev) => new Set(prev).add(suggestion.id));
  };

  const adoptAll = () => {
    suggestions.forEach((suggestion) => {
      if (!adopted.has(suggestion.id)) {
        adoptSuggestion(suggestion);
      }
    });
  };

  if (loading) {
    return (
      <div className="task-suggestions">
        <div className="task-suggestions-header">
          <Sparkles size={16} className="task-suggestions-icon sparkle-animate" />
          <span>AI 正在分析需求...</span>
        </div>
        <div className="task-suggestions-loading">
          <div className="task-suggestions-skeleton"></div>
          <div className="task-suggestions-skeleton"></div>
          <div className="task-suggestions-skeleton"></div>
        </div>
      </div>
    );
  }

  if (suggestions.length === 0) return null;

  const allAdopted = suggestions.every((s) => adopted.has(s.id));

  return (
    <div className="task-suggestions">
      <div className="task-suggestions-header">
        <Sparkles size={16} className="task-suggestions-icon" />
        <span>AI 建议的任务步骤</span>
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
          className="task-suggestions-close"
          onClick={onDismiss}
        >
          <X size={14} />
        </button>
      </div>

      <div className="task-suggestions-list">
        {suggestions.map((suggestion) => {
          const isAdopted = adopted.has(suggestion.id);
          return (
            <div
              key={suggestion.id}
              className={`task-suggestion-item ${isAdopted ? "adopted" : ""}`}
            >
              <div className="task-suggestion-content">
                <div className="task-suggestion-text">
                  {suggestion.content}
                </div>
                <div className="task-suggestion-meta">
                  <span className="task-suggestion-reason">{suggestion.reason}</span>
                  <span className="task-suggestion-confidence">
                    {Math.round(suggestion.confidence * 100)}% 置信度
                  </span>
                </div>
              </div>
              <button
                type="button"
                className="task-suggestion-adopt"
                onClick={() => adoptSuggestion(suggestion)}
                disabled={isAdopted}
                title={isAdopted ? "已采纳" : "采纳建议"}
              >
                {isAdopted ? <Check size={16} /> : "✓"}
              </button>
            </div>
          );
        })}
      </div>

      {allAdopted && (
        <div className="task-suggestions-footer">
          ✨ 所有建议已采纳！祝你工作顺利！
        </div>
      )}
    </div>
  );
}

/**
 * Generate task suggestions based on user prompt
 * In production, this would call a backend API with LLM
 */
function generateSuggestions(prompt: string): TaskSuggestion[] {
  const lowerPrompt = prompt.toLowerCase();

  // Pattern matching for common scenarios
  if (lowerPrompt.includes("bug") || lowerPrompt.includes("修复") || lowerPrompt.includes("fix")) {
    return [
      {
        id: "1",
        content: "定位问题根因并复现",
        reason: "理解问题是解决问题的第一步",
        confidence: 0.95,
      },
      {
        id: "2",
        content: "分析相关代码和依赖",
        reason: "找出所有相关影响点",
        confidence: 0.9,
      },
      {
        id: "3",
        content: "设计修复方案",
        reason: "选择最优解决路径",
        confidence: 0.85,
      },
      {
        id: "4",
        content: "实现修复代码",
        reason: "执行修复方案",
        confidence: 0.9,
      },
      {
        id: "5",
        content: "添加回归测试",
        reason: "防止问题再次发生",
        confidence: 0.88,
      },
    ];
  }

  if (lowerPrompt.includes("feature") || lowerPrompt.includes("功能") || lowerPrompt.includes("开发")) {
    return [
      {
        id: "1",
        content: "分析需求和用户场景",
        reason: "明确功能目标和范围",
        confidence: 0.92,
      },
      {
        id: "2",
        content: "设计 API 接口和数据结构",
        reason: "奠定技术架构基础",
        confidence: 0.88,
      },
      {
        id: "3",
        content: "实现核心功能逻辑",
        reason: "完成主要功能代码",
        confidence: 0.9,
      },
      {
        id: "4",
        content: "编写单元测试和集成测试",
        reason: "保证代码质量",
        confidence: 0.85,
      },
      {
        id: "5",
        content: "更新文档和使用示例",
        reason: "方便其他人使用",
        confidence: 0.8,
      },
    ];
  }

  if (lowerPrompt.includes("refactor") || lowerPrompt.includes("重构")) {
    return [
      {
        id: "1",
        content: "审查现有代码并识别问题",
        reason: "了解当前状态",
        confidence: 0.9,
      },
      {
        id: "2",
        content: "设计新的架构方案",
        reason: "规划重构目标",
        confidence: 0.85,
      },
      {
        id: "3",
        content: "逐步重构（小步快跑）",
        reason: "降低风险",
        confidence: 0.88,
      },
      {
        id: "4",
        content: "确保所有测试通过",
        reason: "验证功能完整性",
        confidence: 0.92,
      },
      {
        id: "5",
        content: "清理旧代码和依赖",
        reason: "完成重构收尾",
        confidence: 0.8,
      },
    ];
  }

  // Generic breakdown
  return [
    {
      id: "1",
      content: "理解和分析需求",
      reason: "明确任务目标",
      confidence: 0.9,
    },
    {
      id: "2",
      content: "设计实施方案",
      reason: "规划执行路径",
      confidence: 0.85,
    },
    {
      id: "3",
      content: "执行主要工作",
      reason: "完成核心任务",
      confidence: 0.88,
    },
    {
      id: "4",
      content: "测试和验证结果",
      reason: "确保质量",
      confidence: 0.86,
    },
    {
      id: "5",
      content: "文档和总结",
      reason: "记录和分享",
      confidence: 0.75,
    },
  ];
}
