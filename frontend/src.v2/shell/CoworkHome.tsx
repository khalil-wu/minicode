import { Bug, FileCheck2, Hammer, SearchCode } from "lucide-react";
import { BrandMark } from "../components/icons";
import { Composer } from "../composer/Composer";
import { useAppStore } from "../stores";

const suggestions = [
  { icon: SearchCode, label: "探索并理解代码", prompt: "请帮我探索并理解这个项目的结构和关键模块。" },
  { icon: Hammer, label: "构建新功能、应用或工具", prompt: "请基于当前项目构建一个新功能，并先梳理实现方案。" },
  { icon: FileCheck2, label: "审查代码并提出修改建议", prompt: "请审查当前代码，找出最值得优化的问题并给出修改建议。" },
  { icon: Bug, label: "修复问题和失败", prompt: "请调查当前项目中的问题或失败，并完成修复。" },
];

export const CoworkHome = () => {
  const setDraft = useAppStore((state) => state.setDraft);
  const chooseSuggestion = (prompt: string) => {
    setDraft(prompt);
    queueMicrotask(() => window.dispatchEvent(new Event("composer:focus")));
  };

  return (
    <div className="workbench-home mc-main-surface">
      <div className="workbench-home-layout">
        <section className="workbench-home-main">
          <div className="workbench-empty-brand">
            <div className="workbench-empty-mark" aria-hidden="true">
              <BrandMark size={22} />
            </div>
            <h1 className="workbench-empty-title">我们应该在 MiniCode 中构建什么？</h1>
            <div className="workbench-home-suggestion-grid" aria-label="任务建议">
              {suggestions.map(({ icon: Icon, label, prompt }) => (
                <button
                  key={label}
                  type="button"
                  className="workbench-home-suggestion-card"
                  onClick={() => chooseSuggestion(prompt)}
                >
                  <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
                  <span>{label}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="workbench-home-composer">
            <Composer minimal />
          </div>
        </section>
      </div>
    </div>
  );
};
