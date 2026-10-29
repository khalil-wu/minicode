import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { invalidateConversationDraftSelection } from "../stores/conversation-drafts";
import type { SkillInfo } from "../stores/types";
import { authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { normalizeSkillInfo } from "../lib/catalog-normalizers";
import { sendClientCommand } from "../protocol/ws-outbox";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { captureExtensionSettingsOwner, extensionSettingsOwnerMatches, publishExtensionSettingsChange, subscribeExtensionSettingsChanges, type ExtensionSettingsOwner } from "../lib/extension-settings-events";
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
  const source = useRef({}).current;
  const mounted = useRef(false);
  const pending = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); }; }, []);
  useEffect(() => { setSkills([]); setPluginSkills([]); setError(""); }, [workspaceRoot]);
  const receive = useCallback((payload: SkillsSettingsPayload) => {
    const normalize = (skill: SettingsSkill): SettingsSkill => ({
      ...normalizeSkillInfo(skill, payload.workspace_root)!, path: skill.path, enabled: skill.enabled, uninstall_name: skill.uninstall_name,
    });
    setSkills(payload.skills.map(normalize));
    setPluginSkills(payload.plugin_skills.map(normalize));
    onSkillsChange?.(payload.skills.length);
  }, [onSkillsChange]);
  const reload = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const root = useAppStore.getState().workingDirectory;
    const owner = captureExtensionSettingsOwner();
    const ownsRead = () => !controller.signal.aborted && request.current === controller
      && workspaceRootsEqual(root, useAppStore.getState().workingDirectory) && extensionSettingsOwnerMatches(owner);
    setLoading(true); setError("");
    try {
      const response = await fetchWithTimeout(`${owner.base}/api/skills/settings?workspace_root=${encodeURIComponent(root)}`, {
        headers: authHeaders(), cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), t("读取技能设置失败")));
      const payload = await response.json() as SkillsSettingsPayload;
      if (ownsRead()) { receive(payload); return payload; }
    } catch (cause) {
      if (ownsRead()) setError(cause instanceof Error ? cause.message : String(cause));
    } finally { if (ownsRead()) setLoading(false); }
  }, [receive, t]);
  useEffect(() => { if (active) void reload(); return () => request.current?.abort(); }, [active, workspaceRoot, reload]);
  useEffect(() => subscribeExtensionSettingsChanges("skills", source, () => {
    request.current?.abort();
    if (active) void reload();
  }), [active, reload, source]);
  const settingsCommitted = async (owner: ExtensionSettingsOwner) => {
    publishExtensionSettingsChange("skills", owner, source);
    if (!extensionSettingsOwnerMatches(owner)) return;
    const store = useAppStore.getState();
    if (store.activeConversationDraftId) void invalidateConversationDraftSelection(store.activeConversationDraftId);
    else sendClientCommand({ type: "skills.list", conversation_id: store.conversationId || undefined,
      workspace_root: store.workingDirectory }, { silent: true });
    if (!mounted.current) return;
    return reload();
  };
  const toggle = async (skill: SettingsSkill) => {
    if (pending.current) return;
    pending.current = true;
    setPendingPath(skill.path);
    const root = workspaceRoot;
    const owner = captureExtensionSettingsOwner();
    const enabled = !skill.enabled;
    try {
      const response = await fetchWithTimeout(`${owner.base}/api/skills/settings`, {
        method: "PUT", headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ path: skill.path, enabled, workspace_root: root }),
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), t("更新技能设置失败")));
      const payload = await settingsCommitted(owner);
      if (extensionSettingsOwnerMatches(owner)) {
        if (!workspaceRootsEqual(root, useAppStore.getState().workingDirectory)) pushToast(`${t("已保存")} · ${skill.display_name || skill.name}`, "success");
        else if (payload && workspaceRootsEqual(payload.workspace_root, root)
          && [...payload.skills, ...payload.plugin_skills].find(item => item.path === skill.path)?.enabled !== enabled) {
          pushToast(t("技能设置已保存，但当前配置尚未生效。"), "warning");
        }
      }
    } catch (cause) { pushToast(cause instanceof Error ? cause.message : String(cause), "error"); }
    finally { pending.current = false; if (mounted.current) setPendingPath(""); }
  };
  const uninstall = async (skill: SettingsSkill) => {
    if (pending.current) return false;
    pending.current = true;
    setPendingPath(skill.path);
    const owner = captureExtensionSettingsOwner();
    try {
      const response = await fetchWithTimeout(`${owner.base}/api/skills/${encodeURIComponent(skill.uninstall_name!)}`, {
        method: "DELETE", headers: authHeaders(),
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), t("卸载")));
      if (extensionSettingsOwnerMatches(owner)) {
        const store = useAppStore.getState();
        store.setMarketplaceSkills(store.marketplaceSkills.map(item => item.name === skill.uninstall_name ? { ...item, installed: false } : item));
      }
      await settingsCommitted(owner);
      return true;
    } catch (cause) { pushToast(cause instanceof Error ? cause.message : String(cause), "error"); return false; }
    finally { pending.current = false; if (mounted.current) setPendingPath(""); }
  };
  return { workspaceRoot, skills, pluginSkills, loading, error, pendingPath, reload, toggle, uninstall, settingsCommitted };
}
