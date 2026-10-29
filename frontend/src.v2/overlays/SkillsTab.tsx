import { useEffect, useState } from "react";
import { useI18n } from "../i18n";
import { BrandIcon } from "../components/BrandIcon";
import { useSkillSettings } from "./useSkillSettings";
import { InstalledSkillDetailsDialog } from "./InstalledSkillDetailsDialog";

export function SkillsTab({ active = true, search = "", onSkillsChange }: {
  active?: boolean; search?: string; onSkillsChange?: (count: number) => void;
}) {
  const { t: tr } = useI18n();
  const { workspaceRoot, skills, loading, error, pendingPath, reload, toggle, uninstall } = useSkillSettings(active, onSkillsChange);
  const [detailPath, setDetailPath] = useState<string | null>(null);
  useEffect(() => { setDetailPath(null); }, [workspaceRoot]);
  const selected = skills.find(skill => skill.path === detailPath);
  const query = search.trim().toLocaleLowerCase();
  const visible = skills.filter(skill => `${skill.name} ${skill.display_name || ""} ${skill.description} ${skill.short_description || ""}`.toLocaleLowerCase().includes(query));
  return <div className="settings-skills-inventory" aria-label={tr("已安装技能")}>
    {error && <div className="settings-load-state" role="alert"><div className="settings-load-state-copy"><strong>{tr("读取技能设置失败")}</strong><code>{error}</code></div>
      <button type="button" className="settings-action-button" onClick={() => void reload()} disabled={loading}>{tr("重试")}</button></div>}
    {loading && skills.length === 0 && <div className="settings-mcp-empty" aria-live="polite">{tr("正在读取技能…")}</div>}
    {visible.length > 0 && <div className="settings-card settings-skill-list">{visible.map(skill => <article key={skill.path} className="settings-skill-row">
      <span className="settings-skill-icon" aria-hidden="true"><BrandIcon value={skill.display_name || skill.name} inferBrand={false} fallback="skill" size={20} iconUrl={skill.icon_large || skill.icon} /></span>
      <button type="button" className="settings-skill-copy settings-skill-open" aria-label={`${tr("查看技能详情")} ${skill.display_name || skill.name}`} onClick={() => setDetailPath(skill.path)}><strong>{skill.display_name || skill.name}</strong><span>{skill.short_description || skill.description}</span></button>
      <button type="button" className="settings-toggle" role="switch" aria-checked={skill.enabled} data-active={skill.enabled}
        aria-label={`${skill.enabled ? tr("停用") : tr("启用")} ${skill.display_name || skill.name}`} disabled={Boolean(pendingPath) || loading} onClick={() => void toggle(skill)}><span /></button>
    </article>)}</div>}
    {!loading && !error && visible.length === 0 && <div className="settings-mcp-empty">{query ? tr("没有匹配的技能。") : tr("没有已安装技能")}</div>}
    {active && selected && <InstalledSkillDetailsDialog skill={selected} workspaceRoot={workspaceRoot} pending={Boolean(pendingPath) || loading} onToggle={() => void toggle(selected)}
      onUninstall={() => uninstall(selected)} onClose={() => setDetailPath(null)} />}
  </div>;
}
