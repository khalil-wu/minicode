import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import type { SkillInfo } from "../stores/types";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { normalizeSkillInfo, normalizeSkillList } from "../lib/catalog-normalizers";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { useI18n } from "../i18n";
import { pushToast } from "./ToastContainer";
import type { SettingsSkill } from "./SkillDetailsDialog";

export type SkillsSettingsPayload = { skills: SettingsSkill[]; plugin_skills: SettingsSkill[]; available_skills: SkillInfo[]; workspace_root: string };

export function useSkillSettings(active: boolean, onSkillsChange?: (count: number) => void) {
  const { t } = useI18n();
  const workspaceRoot = useAppStore(state => state.workingDirectory);
  const [skills, setSkills] = useState<SettingsSkill[]>([]);
  const [pluginSkills, setPluginSkills] = useState<SettingsSkill[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [pendingPath, setPendingPath] = useState("");
  const request = useRef<AbortController>();
  useEffect(() => { setSkills([]); setPluginSkills([]); setError(""); }, [workspaceRoot]);
  const receive = useCallback((payload: SkillsSettingsPayload) => {
    const normalize = (skill: SettingsSkill): SettingsSkill => ({
      ...normalizeSkillInfo(skill, payload.workspace_root)!, path: skill.path, enabled: skill.enabled,
    });
    setSkills(payload.skills.map(normalize));
    setPluginSkills(payload.plugin_skills.map(normalize));
    onSkillsChange?.(payload.skills.length);
  }, [onSkillsChange]);
  const reload = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    setLoading(true); setError("");
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/skills/settings?workspace_root=${encodeURIComponent(workspaceRoot)}`, {
        headers: authHeaders(), cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), t("读取技能设置失败")));
      const payload = await response.json() as SkillsSettingsPayload;
      if (!controller.signal.aborted) receive(payload);
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }, [workspaceRoot, receive, t]);
  useEffect(() => { if (active) void reload(); return () => request.current?.abort(); }, [active, reload]);
  const toggle = async (skill: SettingsSkill) => {
    setPendingPath(skill.path);
    const root = workspaceRoot;
    try {
      const response = await fetchWithTimeout(`${apiBase()}/api/skills/settings`, {
        method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ path: skill.path, enabled: !skill.enabled, workspace_root: root }),
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), t("更新技能设置失败")));
      const payload = await response.json() as SkillsSettingsPayload;
      if (workspaceRootsEqual(root, useAppStore.getState().workingDirectory)) {
        receive(payload);
        useAppStore.getState().setAvailableSkills(normalizeSkillList(payload.available_skills, root));
      }
    } catch (cause) { pushToast(cause instanceof Error ? cause.message : String(cause), "error"); }
    finally { setPendingPath(""); }
  };
  return { workspaceRoot, skills, pluginSkills, loading, error, pendingPath, reload, toggle };
}
