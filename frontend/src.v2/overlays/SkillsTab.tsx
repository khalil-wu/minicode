import { Check, RefreshCw, Sparkles } from "lucide-react";
import { useState } from "react";
import { BrandIcon } from "../components/BrandIcon";
import {
  commandResultSucceeded,
  sendClientCommandAwaitResult,
} from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { Section } from "./settingsShared";
import { pushToast } from "./ToastContainer";
import { workspaceFilePathComparisonKey } from "../lib/workspace-path";

// Mirrors the backend's source_level vocabulary
// (backend/skills/loader.py: managed / plugin / user / workspace / builtin).
const sourceLabel = (source?: string) => ({
  managed: "受管",
  plugin: "插件",
  user: "个人",
  workspace: "工作区",
  builtin: "内置",
}[source ?? ""] ?? source ?? "本地");

export const SkillsTab = ({ onReturnToApp }: { onReturnToApp: () => void }) => {
  const availableSkills = useAppStore((state) => state.availableSkills);
  const selectedSkills = useAppStore((state) => state.selectedSkills);
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const addSelectedSkill = useAppStore((state) => state.addSelectedSkill);
  const toggleSkillsMarketplace = useAppStore((state) => state.toggleSkillsMarketplace);
  const [refreshing, setRefreshing] = useState(false);
  const skillKey = (path: string | undefined, name: string): string => path
    ? `path:${workspaceFilePathComparisonKey(path, workingDirectory)}`
    : `name:${name}`;
  const selectedKeys = new Set(selectedSkills.map((skill) => skillKey(skill.path, skill.name)));

  const openSkillManager = () => {
    toggleSkillsMarketplace("settings");
  };

  const refreshSkills = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const result = await sendClientCommandAwaitResult(
        { type: "skills.list" },
        "skills.list",
      );
      if (!commandResultSucceeded(result)) {
        pushToast(`刷新技能失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      pushToast("技能列表已刷新", "success");
    } catch (error) {
      pushToast(`刷新技能失败：${error instanceof Error ? error.message : String(error)}`, "error");
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <>
      <Section title="技能" description="SKILL.md 工作流，按任务调用。">
        <div className="settings-skill-summary">
          <span className="settings-skill-summary-icon" aria-hidden="true"><Sparkles /></span>
          <div>
            <strong>{availableSkills.length} 个可用技能</strong>
            <span>{selectedSkills.length > 0 ? `${selectedSkills.length} 个已用于下一条消息` : "可在消息中输入 $ 选择技能"}</span>
          </div>
          <button type="button" className="settings-icon-button" onClick={() => void refreshSkills()} disabled={refreshing} aria-label="刷新技能" title="刷新技能">
            <RefreshCw className={refreshing ? "settings-spin" : undefined} />
          </button>
          <button type="button" className="settings-action-button" data-primary="true" onClick={openSkillManager}>管理技能</button>
        </div>

        <div className="settings-skill-list">
          {availableSkills.length > 0 ? availableSkills.map((skill) => {
            const key = skillKey(skill.path, skill.name);
            const selected = selectedKeys.has(key);
            return (
              <article className="settings-skill-row" key={key}>
                <span className="settings-skill-icon" aria-hidden="true">
                  <BrandIcon value={skill.display_name || skill.name} fallback="skill" size={20} iconUrl={skill.icon} />
                </span>
                <div className="settings-skill-copy">
                  <div>
                    <strong>{skill.display_name || skill.name}</strong>
                    <span>{sourceLabel(skill.source_level)}</span>
                    {skill.mcp_dependencies?.length ? <span>{skill.mcp_dependencies.length} 个 MCP 依赖</span> : null}
                  </div>
                  <p>{skill.short_description || skill.description || "暂无说明"}</p>
                </div>
                {skill.user_invocable !== false ? <button
                  type="button"
                  className="settings-action-button"
                  disabled={selected}
                  onClick={() => {
                    addSelectedSkill({
                      name: skill.name,
                      path: skill.path,
                      description: skill.description,
                      sourceLevel: skill.source_level,
                    });
                    onReturnToApp();
                  }}
                >
                  {selected ? <><Check />已选</> : "使用"}
                </button> : null}
              </article>
            );
          }) : (
            <div className="settings-skill-empty">
              <Sparkles aria-hidden="true" />
              <strong>没有已安装技能</strong>
              <button type="button" className="settings-action-button" onClick={openSkillManager}>打开技能目录</button>
            </div>
          )}
        </div>
      </Section>
    </>
  );
};
