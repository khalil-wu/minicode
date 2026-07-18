import { useState } from "react";
import {
  Bug,
  ChevronRight,
  FileText,
  FlaskConical,
  Gauge,
  RefreshCcw,
  Rocket,
  ScanSearch,
  Sparkles,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useAppStore } from "../../stores";
import { createTodoItem } from "../../lib/todo-utils";
import "./task-templates.css";

interface TaskTemplate {
  id: string;
  name: string;
  icon: LucideIcon;
  description: string;
  tasks: string[];
}

const TEMPLATES: TaskTemplate[] = [
  {
    id: "bug-fix",
    name: "Bug 修复",
    icon: Bug,
    description: "标准的 bug 修复流程",
    tasks: [
      "定位 bug 根因",
      "设计修复方案",
      "实现修复代码",
      "添加回归测试",
      "验证修复效果",
    ],
  },
  {
    id: "feature-dev",
    name: "功能开发",
    icon: Sparkles,
    description: "新功能开发完整流程",
    tasks: [
      "分析需求",
      "设计接口和数据结构",
      "实现核心功能",
      "编写单元测试",
      "更新文档",
    ],
  },
  {
    id: "refactor",
    name: "代码重构",
    icon: RefreshCcw,
    description: "安全重构代码",
    tasks: [
      "审查现有代码",
      "设计新架构",
      "逐步重构",
      "确保测试通过",
      "清理旧代码",
    ],
  },
  {
    id: "code-review",
    name: "代码审查",
    icon: ScanSearch,
    description: "全面审查代码质量",
    tasks: [
      "检查代码风格",
      "审查逻辑正确性",
      "检查安全问题",
      "验证测试覆盖",
      "提出改进建议",
    ],
  },
  {
    id: "optimization",
    name: "性能优化",
    icon: Gauge,
    description: "系统性能优化",
    tasks: [
      "性能分析和瓶颈定位",
      "设计优化方案",
      "实现优化",
      "基准测试对比",
      "文档记录",
    ],
  },
  {
    id: "documentation",
    name: "文档编写",
    icon: FileText,
    description: "完整项目文档",
    tasks: [
      "编写 README",
      "API 文档",
      "使用示例",
      "架构设计文档",
      "常见问题 FAQ",
    ],
  },
  {
    id: "testing",
    name: "测试覆盖",
    icon: FlaskConical,
    description: "提升测试覆盖率",
    tasks: [
      "分析测试覆盖情况",
      "编写单元测试",
      "编写集成测试",
      "添加 E2E 测试",
      "修复失败测试",
    ],
  },
  {
    id: "deployment",
    name: "部署上线",
    icon: Rocket,
    description: "生产环境部署",
    tasks: [
      "代码审查",
      "运行全部测试",
      "构建生产版本",
      "部署到生产环境",
      "监控和验证",
    ],
  },
];

interface TaskTemplatesProps {
  onApply?: () => void;
}

export function TaskTemplates({ onApply }: TaskTemplatesProps) {
  const [expanded, setExpanded] = useState(false);
  const addTodo = useAppStore((s) => s.addTodo);

  const applyTemplate = (template: TaskTemplate) => {
    template.tasks.forEach((task) => {
      addTodo(createTodoItem(task));
    });
    setExpanded(false);
    onApply?.();
  };

  if (!expanded) {
    return (
      <button
        type="button"
        className="task-templates-trigger"
        onClick={() => setExpanded(true)}
        title="使用任务模板"
      >
        <FileText size={14} />
        <span>模板</span>
        <ChevronRight size={14} />
      </button>
    );
  }

  return (
    <div className="task-templates">
      <div className="task-templates-header">
        <FileText size={16} />
        <span>选择任务模板</span>
        <button
          type="button"
          className="task-templates-close mc-icon-button mc-icon-button-compact"
          onClick={() => setExpanded(false)}
          title="关闭模板"
          aria-label="关闭模板选择"
        >
          <X size={14} />
        </button>
      </div>

      <div className="task-templates-grid">
        {TEMPLATES.map((template) => {
          const TemplateIcon = template.icon;
          return (
            <button
              key={template.id}
              type="button"
              className="task-template-card"
              onClick={() => applyTemplate(template)}
            >
              <div className="task-template-icon" aria-hidden="true">
                <TemplateIcon size={20} strokeWidth={1.75} />
              </div>
              <div className="task-template-content">
                <div className="task-template-name">{template.name}</div>
                <div className="task-template-description">{template.description}</div>
                <div className="task-template-count">{template.tasks.length} 个任务</div>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
