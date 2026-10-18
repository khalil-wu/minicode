import { useCallback, useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import type { GitPreferences } from "../protocol/git-preferences";

type InstructionKey = "commit_instructions" | "pull_request_instructions";

export function GitInstructionField({ field, title, subtitle, placeholder, value, onSave }: {
  field: InstructionKey; title: string; subtitle: string; placeholder: string; value: string;
  onSave: (updates: Partial<GitPreferences>) => Promise<boolean>;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const latest = useRef<string | null>(null);
  const save = useRef(onSave); save.current = onSave;
  const submitted = useRef<string | null>(null);
  const queue = useRef(Promise.resolve(true));
  const flush = useCallback((manual = false) => {
    const text = latest.current;
    if (text === null || (!manual && text === submitted.current)) return;
    submitted.current = text;
    setSaving(true); setSaved(false);
    // The desktop serializes mutations for each instruction field, so a slow
    // earlier save cannot overwrite the user's next edit.
    const submission = queue.current.then(() => save.current({ [field]: text }));
    queue.current = submission;
    void submission.then((success) => {
      if (success && latest.current === text) { latest.current = null; setDraft(null); setSaved(true); }
      if (queue.current === submission) setSaving(false);
    });
  }, [field]);
  useEffect(() => {
    if (draft === null) return;
    const timer = window.setTimeout(() => flush(), 3000);
    return () => window.clearTimeout(timer);
  }, [draft, flush]);
  useEffect(() => () => flush(), [flush]);
  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), 3000);
    return () => window.clearTimeout(timer);
  }, [saved]);
  return <section className="settings-group settings-git-instructions">
    <div className="settings-git-instructions-header"><div><h3 className="settings-group-title">{title}</h3><p className="settings-section-description">{subtitle}</p></div>
      {(draft !== null || saving) ? <button type="button" className="settings-action-button" disabled={saving} onClick={() => flush(true)}>{saving ? t("保存中…") : t("保存")}</button>
        : saved && <span className="settings-git-instructions-saved" role="status">{t("已保存")}</span>}
    </div>
    <textarea rows={6} aria-label={title} placeholder={placeholder} value={draft ?? value} onChange={(event) => {
      const text = event.target.value;
      setSaved(false); submitted.current = null;
      latest.current = text === value && !saving ? null : text;
      setDraft(latest.current);
    }} onKeyDown={(event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); flush(true); }
    }} />
  </section>;
}
