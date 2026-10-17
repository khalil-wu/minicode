import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "../lib/icons";
import { useI18n } from "../i18n";
import { fetchCodeReviewOptions } from "../protocol/code-review";
import type { CodeReviewOptions } from "../protocol/code-review-types";
import "./CodeReviewMenu.css";

export interface CodeReviewOwner { conversationId?: string; cwd: string; input: string; selectedCommand: string | null; dismissed?: boolean; }

export function CodeReviewMenu({ owner, pending, onClose, onSelect }: {
  owner: CodeReviewOwner; pending: boolean; onClose: () => void;
  onSelect: (mode: "uncommitted" | "base-branch", options: CodeReviewOptions, branch?: string) => void;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const [options, setOptions] = useState<CodeReviewOptions>();
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setOptions(undefined); setError("");
    fetchCodeReviewOptions(owner.conversationId, owner.cwd).then(value => { if (active) setOptions(value); })
      .catch(reason => { if (active) setError(reason.message); });
    return () => { active = false; };
  }, [owner, revision]);
  useEffect(() => {
    const pointer = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) onClose(); };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); return; }
      if (!["ArrowDown", "ArrowUp", "Home", "End", "Enter"].includes(event.key)) return;
      const buttons = [...(ref.current?.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)') ?? [])];
      if (!buttons.length) return;
      event.preventDefault(); event.stopPropagation();
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      if (event.key === "Enter") { buttons[Math.max(0, index)].click(); return; }
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
        : event.key === "ArrowUp" ? (index + buttons.length - 1) % buttons.length : (index + 1) % buttons.length;
      buttons[next].focus();
    };
    document.addEventListener("pointerdown", pointer);
    document.addEventListener("keydown", key, true);
    return () => { document.removeEventListener("pointerdown", pointer); document.removeEventListener("keydown", key, true); };
  }, [onClose]);
  return <div ref={ref} className="code-review-menu" role="menu" aria-label={t("代码审阅")} data-code-review-menu>
    {error ? <div className="code-review-menu-error"><span>{error}</span><button type="button" onClick={() => setRevision(value => value + 1)}>{t("重试")}</button></div>
      : !options ? <div className="code-review-menu-loading"><LoaderCircle size={15} className="animate-spin" />{t("正在加载分支…")}</div>
        : <>
          <button type="button" role="menuitem" disabled={pending} data-review-target="uncommitted" onClick={() => onSelect("uncommitted", options)}>
            <span>{t("审阅未提交的更改")}</span>{pending && <LoaderCircle size={15} className="animate-spin" />}
          </button>
          <div className="code-review-menu-section">{t("与基础分支比较")}</div>
          {options.comparisonStatus === "expected" && <div className="code-review-menu-section" role="status"
            data-comparison-status={options.comparisonStatus} data-comparison-reason={options.reason}>{t("选择分支")}</div>}
          {options.branches.map(branch => <button key={branch} type="button" role="menuitem" disabled={pending} data-review-target="base-branch" data-review-branch={branch} onClick={() => onSelect("base-branch", options, branch)}>
            <span>{branch}</span>
          </button>)}
        </>}
  </div>;
}
