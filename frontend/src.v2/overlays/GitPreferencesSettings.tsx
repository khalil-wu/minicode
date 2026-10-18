import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import type { GitPreferences } from "../protocol/git-preferences";
import { GitInstructionField } from "./GitInstructionField";
import "./GitPreferencesSettings.css";

export function GitPreferencesSettings({ active = true }: { active?: boolean }) {
  const { t } = useI18n();
  const [preferences, setPreferences] = useState<GitPreferences | null>(null);
  const [prefix, setPrefix] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const savedRevision = useRef(0);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const revision = savedRevision.current;
    void fetchWithTimeout(new URL("/api/settings/git", apiBase()), { headers: authHeaders(), signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
        return response.json() as Promise<GitPreferences>;
      }).then((next) => { if (!controller.signal.aborted && revision === savedRevision.current) setPreferences(next); })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => controller.abort();
  }, [active]);
  const save = async (updates: Partial<GitPreferences>) => {
    setSaving(true); setError("");
    try {
      const response = await fetchWithTimeout(new URL("/api/settings/git", apiBase()), {
        method: "PUT", headers: authHeaders({ "content-type": "application/json" }), body: JSON.stringify(updates),
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      savedRevision.current += 1;
      // Each response acknowledges one setting patch. Replacing the complete
      // form with that response can undo another instruction field's newer save.
      setPreferences((current) => ({ ...current!, ...updates }));
      if ("branch_prefix" in updates) setPrefix((draft) => draft === updates.branch_prefix ? null : draft);
      if ("branch_prefix" in updates || "always_force_push" in updates || "create_draft_pull_requests" in updates) window.dispatchEvent(new Event("minicode-git-preferences-changed"));
      return true;
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); return false; }
    finally { setSaving(false); }
  };
  const savePrefix = () => {
    if (prefix !== null && prefix !== preferences?.branch_prefix && !saving) void save({ branch_prefix: prefix });
  };
  return <><section className="settings-group settings-git-preferences" aria-label={t("Git 偏好")}>
    <div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("分支前缀")}</div>
        <div className="settings-row-description">{t("MiniCode 创建新分支时使用的前缀")}</div></div>
        <div className="settings-row-control"><input className="settings-git-prefix" aria-label={t("分支前缀")} placeholder="codex/" value={prefix ?? preferences?.branch_prefix ?? ""} disabled={!preferences || saving}
          onChange={(event) => setPrefix(event.target.value)} onBlur={savePrefix} onKeyDown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); savePrefix(); }
          }} /></div></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("Pull Request 合并方式")}</div>
        <div className="settings-row-description">{t("选择 MiniCode 合并 Pull Request 的方式")}</div></div>
        <div role="group" aria-label={t("Pull Request 合并方式")} className="settings-git-segments">
          {([['merge', t("合并")], ['squash', t("压缩合并")]] as const).map(([method, label]) => <button type="button" key={method}
            aria-pressed={preferences?.pull_request_merge_method === method} disabled={!preferences || saving}
            onClick={() => void save({ pull_request_merge_method: method })}>{label}</button>)}
        </div></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("始终强制推送")}</div>
        <div className="settings-row-description">{t("从 MiniCode 推送时使用 --force-with-lease")}</div></div>
        <button type="button" className="settings-toggle" role="switch" aria-label={t("始终强制推送")} aria-checked={preferences?.always_force_push ?? false} data-active={preferences?.always_force_push ?? false} disabled={!preferences || saving}
          onClick={() => void save({ always_force_push: !preferences!.always_force_push })}><span /></button></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("创建草稿 Pull Request")}</div>
        <div className="settings-row-description">{t("从 MiniCode 创建 PR 时默认使用草稿")}</div></div>
        <button type="button" className="settings-toggle" role="switch" aria-label={t("创建草稿 Pull Request")} aria-checked={preferences?.create_draft_pull_requests ?? true} data-active={preferences?.create_draft_pull_requests ?? true} disabled={!preferences || saving}
          onClick={() => void save({ create_draft_pull_requests: !preferences!.create_draft_pull_requests })}><span /></button></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("审查结果呈现方式")}</div>
        <div className="settings-row-description">{t("尽可能在当前聊天中启动 /review，或启动单独的审查聊天")}</div></div>
        <div role="group" aria-label={t("审查结果呈现方式")} className="settings-git-segments">
          {([['inline', t("内嵌")], ['detached', t("单独")]] as const).map(([delivery, label]) => <button type="button" key={delivery}
            aria-pressed={preferences?.review_delivery === delivery} disabled={!preferences || saving}
            onClick={() => void save({ review_delivery: delivery })}>{label}</button>)}
        </div></div>
    </div>
    {error && <p className="settings-inline-error" role="alert">{error}</p>}
  </section>
    {preferences && <>
      <GitInstructionField field="commit_instructions" title={t("提交指令")} subtitle={t("添加到提交说明生成提示词中")} placeholder={t("添加提交说明指引…")} value={preferences.commit_instructions} onSave={save} />
      <GitInstructionField field="pull_request_instructions" title={t("Pull Request 指令")} subtitle={t("添加到 PR 标题和说明生成提示词中")} placeholder={t("添加 Pull Request 指引…")} value={preferences.pull_request_instructions} onSave={save} />
    </>}
  </>;
}
