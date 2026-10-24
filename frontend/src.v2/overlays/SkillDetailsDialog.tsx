import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { MoreHorizontal, X } from "../lib/icons";
import { BrandIcon } from "../components/BrandIcon";
import { ContextMenu } from "../components/ContextMenu";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { useI18n } from "../i18n";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import type { SkillInfo } from "../stores/types";
import { copyText } from "../lib/clipboard";
import "./SkillDetailsDialog.css";

export type SettingsSkill = SkillInfo & { path: string; enabled: boolean; uninstall_name?: string | null };

export function SkillDetailsDialog({ skill, workspaceRoot, pending, readOnly = false, canToggle = true, canOpen, canReveal, onOpen, onReveal, onToggle, onUninstall, onTry, onClose }: {
  skill: SettingsSkill; workspaceRoot: string; pending: boolean;
  canOpen: boolean; canReveal: boolean; onOpen: () => void; onReveal: () => void;
  readOnly?: boolean;
  canToggle?: boolean;
  onToggle: () => void; onTry: () => void; onClose: () => void;
  onUninstall?: () => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  const dialog = useFocusTrap(true);
  const [detail, setDetail] = useState<{ content: string; raw_content: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [menu, setMenu] = useState<HTMLElement | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setDetail(null); setError("");
    const load = async () => {
      try {
        const query = new URLSearchParams({ skill_path: skill.path, workspace_root: workspaceRoot });
        const response = await fetchWithTimeout(`${apiBase()}/api/skills/detail?${query}`, {
          headers: authHeaders(), cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), t("读取技能详情失败")));
        const detail = await response.json() as { content: string; raw_content: string };
        if (!controller.signal.aborted) setDetail(detail);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
      } finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load();
    return () => controller.abort();
  }, [skill.path, workspaceRoot, t, reload]);
  const name = skill.display_name || skill.name;
  return createPortal(<div className="skill-details-backdrop" onClick={onClose}>
    <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} className="skill-details-dialog"
      onClick={event => event.stopPropagation()} onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); }
      }}>
      <header className="skill-details-toolbar">
        <BrandIcon value={name} inferBrand={false} fallback="skill" size={40} iconUrl={skill.icon_large || skill.icon} />
        <div>{!readOnly && <>{canToggle && <button type="button" className="settings-toggle" role="switch" aria-checked={skill.enabled} data-active={skill.enabled}
          disabled={pending} aria-label={`${skill.enabled ? t("停用") : t("启用")} ${name}`} onClick={onToggle}><span /></button>}
          <button type="button" className="skill-details-more" aria-label={t("技能更多操作")} title={t("技能更多操作")}
            aria-haspopup="menu" aria-expanded={Boolean(menu)} onClick={event => setMenu(menu ? null : event.currentTarget)}><MoreHorizontal size={18} /></button></>}
          <button type="button" className="skill-details-close" aria-label={t("关闭技能详情")} onClick={onClose}><X size={18} /></button></div>
      </header>
      <div className="skill-details-scroll">
        <h2 id={titleId}>{name}</h2><p className="skill-details-description">{skill.description}</p>
        {loading && <p className="skill-details-loading" aria-live="polite">{t("正在读取技能…")}</p>}
        {error && <div role="alert"><p>{error}</p><button type="button" className="settings-action-button" onClick={() => setReload(value => value + 1)}>{t("重试")}</button></div>}
        {detail && <div className="skill-details-body"><MarkdownRenderer content={detail.content.replace(/^# [^\r\n]*(?:\r?\n|$)/, "")} workspaceRoot={skill.path.replace(/[/\\][^/\\]+$/, "")} /></div>}
      </div>
      <footer>{!readOnly && onUninstall && <button type="button" className="skill-details-uninstall settings-action-button" disabled={pending} onClick={onUninstall}>{t("卸载")}</button>}
        {readOnly ? <button type="button" className="plugin-primary-button" onClick={onClose}>{t("关闭")}</button>
        : <button type="button" className="plugin-primary-button" disabled={!skill.enabled || skill.user_invocable === false || pending} onClick={onTry}>{t("立即试用")}</button>}</footer>
      {!readOnly && menu && <ContextMenu position={{ x: 0, y: 0 }} anchor={menu} onClose={() => setMenu(null)} ariaLabel={t("技能更多操作")} items={[
        { label: t("打开"), disabled: !canOpen, onClick: onOpen },
        { label: t("在文件资源管理器中显示技能"), disabled: !canReveal, onClick: onReveal },
        { label: t("复制技能 Markdown"), disabled: detail === null, onClick: () => void copyText(detail!.raw_content, "SKILL.md", t("已复制 SKILL.md")) },
      ]} />}
    </div>
  </div>, document.body);
}
