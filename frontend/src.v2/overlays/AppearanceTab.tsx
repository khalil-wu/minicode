import { Check, Monitor, Moon, Sun, Type } from "lucide-react";
import { useAppStore } from "../stores";

const THEMES = [
  { id: "system", label: "系统", icon: Monitor },
  { id: "light", label: "浅色", icon: Sun },
  { id: "dark", label: "深色", icon: Moon },
] as const;

const TEXT_SCALES = [
  { value: 0.92, label: "紧凑" },
  { value: 1, label: "默认" },
  { value: 1.12, label: "较大" },
] as const;

const CODE_TEXT_SCALES = [
  { value: 0.9, label: "紧凑" },
  { value: 1, label: "默认" },
  { value: 1.15, label: "较大" },
] as const;

export const AppearanceTab = () => {
  const themeMode = useAppStore((s) => s.themeMode);
  const textScale = useAppStore((s) => s.textScale);
  const codeTextScale = useAppStore((s) => s.codeTextScale);
  const reducedMotion = useAppStore((s) => s.reducedMotion);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const setTextScale = useAppStore((s) => s.setTextScale);
  const setCodeTextScale = useAppStore((s) => s.setCodeTextScale);
  const setReducedMotion = useAppStore((s) => s.setReducedMotion);

  const selectedScale = TEXT_SCALES.reduce((nearest, item) => (
    Math.abs(item.value - textScale) < Math.abs(nearest.value - textScale) ? item : nearest
  ), TEXT_SCALES[0]);
  const selectedCodeScale = CODE_TEXT_SCALES.reduce((nearest, item) => (
    Math.abs(item.value - codeTextScale) < Math.abs(nearest.value - codeTextScale) ? item : nearest
  ), CODE_TEXT_SCALES[0]);

  return (
    <>
      <section className="settings-group settings-appearance-theme">
        <h3 className="settings-group-title">主题</h3>
        <div className="settings-theme-grid" role="radiogroup" aria-label="应用主题">
          {THEMES.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className="settings-theme-card"
              data-theme-preview={id}
              data-active={themeMode === id ? "true" : "false"}
              role="radio"
              aria-checked={themeMode === id}
              aria-label={label}
              onClick={() => setThemeMode(id)}
            >
              <span className="settings-theme-preview" aria-hidden="true">
                <span className="settings-theme-preview-chrome" />
                <span className="settings-theme-preview-sidebar">
                  <i /><i /><i />
                </span>
                <span className="settings-theme-preview-main">
                  <b /><i /><i /><i />
                </span>
                {themeMode === id && <span className="settings-theme-selected"><Check /></span>}
              </span>
              <span className="settings-theme-label"><Icon aria-hidden="true" />{label}</span>
            </button>
          ))}
        </div>
      </section>

      <section className="settings-group">
        <h3 className="settings-group-title">偏好设置</h3>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-copy">
              <div className="settings-row-title">界面字号</div>
              <div className="settings-row-description">调整导航、对话和设置页面的基础字号。</div>
            </div>
            <div className="settings-row-control">
              <div className="settings-segmented" role="radiogroup" aria-label="界面字号">
                {TEXT_SCALES.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    className="settings-segment"
                    data-active={selectedScale.value === option.value ? "true" : "false"}
                    role="radio"
                    aria-checked={selectedScale.value === option.value}
                    onClick={() => setTextScale(option.value)}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <span className="settings-scale-value" aria-label={`当前缩放 ${Math.round(textScale * 100)}%`}>
                <Type aria-hidden="true" /> {Math.round(textScale * 100)}%
              </span>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-copy">
              <div className="settings-row-title">代码字号</div>
              <div className="settings-row-description">单独调整编辑器和代码块字号。</div>
            </div>
            <div className="settings-row-control">
              <div className="settings-segmented" role="radiogroup" aria-label="代码字号">
                {CODE_TEXT_SCALES.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    className="settings-segment"
                    data-active={selectedCodeScale.value === option.value ? "true" : "false"}
                    role="radio"
                    aria-checked={selectedCodeScale.value === option.value}
                    onClick={() => setCodeTextScale(option.value)}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              <span className="settings-scale-value" aria-label={`当前代码缩放 ${Math.round(codeTextScale * 100)}%`}>
                <Type aria-hidden="true" /> {Math.round(codeTextScale * 100)}%
              </span>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-copy">
              <div className="settings-row-title">减少动态效果</div>
              <div className="settings-row-description">关闭界面动画和平滑滚动。</div>
            </div>
            <div className="settings-row-control">
              <button
                type="button"
                className="settings-toggle"
                role="switch"
                aria-checked={reducedMotion}
                aria-label="减少动态效果"
                data-active={reducedMotion ? "true" : "false"}
                onClick={() => setReducedMotion(!reducedMotion)}
              >
                <span />
              </button>
            </div>
          </div>
        </div>
      </section>
    </>
  );
};
