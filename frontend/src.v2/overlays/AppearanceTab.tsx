import { Check, Monitor, Moon, Sun } from "lucide-react";
import { useAppStore } from "../stores";
import type { KeyboardEvent } from "react";
import { EditorPreferencesSettings } from "./EditorPreferencesSettings";

const onRadioGroupKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
  if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]'));
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (current + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1) + buttons.length) % buttons.length;
  buttons[next].focus();
  buttons[next].click();
};

const THEMES = [
  { id: "system", label: "系统", icon: Monitor },
  { id: "light", label: "浅色", icon: Sun },
  { id: "dark", label: "深色", icon: Moon },
] as const;

export const AppearanceTab = () => {
  const themeMode = useAppStore((s) => s.themeMode);
  const reducedMotion = useAppStore((s) => s.reducedMotion);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const setReducedMotion = useAppStore((s) => s.setReducedMotion);

  return (
    <>
      <section className="settings-group settings-appearance-theme">
        <h3 className="settings-group-title">视觉风格</h3>
        <div className="settings-card"><div className="settings-row settings-mode-row">
        <div className="settings-row-copy"><div className="settings-row-title">模式</div></div>
        <div className="settings-theme-grid" role="radiogroup" aria-label="应用主题" onKeyDown={onRadioGroupKeyDown}>
          {THEMES.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className="settings-theme-card"
              data-theme-preview={id}
              data-active={themeMode === id ? "true" : "false"}
              role="radio"
              aria-checked={themeMode === id}
              tabIndex={themeMode === id ? 0 : -1}
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
              <span className="sr-only"><Icon aria-hidden="true" />{label}</span>
            </button>
          ))}
        </div>
        </div></div>
      </section>

      <section className="settings-group">
        <h3 className="settings-group-title">偏好设置</h3>
        <div className="settings-card">
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
      <EditorPreferencesSettings />
    </>
  );
};
