import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { commandResultSucceeded, sendClientCommandAwaitResult, LONG_COMMAND_RESULT_TIMEOUT_MS } from "../protocol/ws-outbox";
import { getWebSocket } from "../hooks/useWebSocket";
import { SelectMenu } from "../components/SelectMenu";
import { isDesktop } from "../desktop/runtime";
import { showConfirm } from "./DialogService";
import { useFocusTrap } from "../hooks/useFocusTrap";
import "./StorageTab.css";

type CategoryName = "chats" | "archived" | "subagents" | "unclassified" | "worktrees" | "other";
type Usage = { path: string; total_bytes: number; categories: { name: CategoryName; bytes: number }[] };
type ReviewChat = { id: string; title: string; updated_at: string; archived: boolean; subagent_count: number; snapshot: Record<string, unknown> };
type Review = { cutoff: string; days: number; archived_only: boolean; chats: ReviewChat[] };
const categoryOrder: CategoryName[] = ["chats", "subagents", "archived", "worktrees", "other"];
const bytes = (value: number, locale: string) => {
  if (value < 1024) return new Intl.NumberFormat(locale, { style: "unit", unit: "byte", unitDisplay: "long" }).format(value);
  const units = ["kilobyte", "megabyte", "gigabyte", "terabyte"];
  let size = value / 1024, index = 0;
  for (; size >= 1024 && index < units.length - 1; index += 1) size /= 1024;
  return new Intl.NumberFormat(locale, { style: "unit", unit: units[index], unitDisplay: "short", maximumFractionDigits: 1 }).format(size);
};

export function StorageTab({ active = true }: { active?: boolean }) {
  const { t, locale } = useI18n();
  const connected = useAppStore(state => state.isConnected);
  const sessionId = connected ? getWebSocket()?.sessionId : undefined;
  const [usage, setUsage] = useState<Usage | null>(null);
  const [measuring, setMeasuring] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [days, setDays] = useState(90);
  const [archivedOnly, setArchivedOnly] = useState(true);
  const [review, setReview] = useState<Review | null>(null);
  const [selected, setSelected] = useState(new Set<string>());
  const [visible, setVisible] = useState(50);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const reviewRef = useFocusTrap(reviewOpen && active);
  const totalLabelId = useId();
  const categoryLabels = { chats: t("聊天"), archived: t("已归档聊天"), subagents: t("子智能体"), unclassified: t("未分类聊天"), worktrees: t("工作树"), other: t("其他") };
  const categories = usage?.categories.flatMap(({ name, bytes: size }) => name === "unclassified" ? [] : [{
    name, bytes: name === "other" ? size + usage.categories.find(category => category.name === "unclassified")!.bytes : size,
  }]).sort((left, right) => categoryOrder.indexOf(left.name) - categoryOrder.indexOf(right.name));
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setError("");
    setMeasuring(true);
    void fetchWithTimeout(new URL("/api/settings/storage", apiBase()), { headers: authHeaders(), signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<Usage>;
    }).then(next => { if (!controller.signal.aborted) setUsage(next); }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); }).finally(() => { if (!controller.signal.aborted) setMeasuring(false); });
    return () => controller.abort();
  }, [active, revision]);
  useEffect(() => {
    if (!active || !reviewOpen || !sessionId) return;
    const controller = new AbortController();
    setError(""); setReview(null); setVisible(50);
    const url = new URL("/api/settings/storage/review", apiBase());
    url.searchParams.set("session_id", sessionId); url.searchParams.set("days", String(days)); url.searchParams.set("archived_only", String(archivedOnly));
    void fetchWithTimeout(url, { headers: authHeaders(), signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<Review>;
    }).then(next => { if (!controller.signal.aborted) { setReview(next); setSelected(new Set(next.chats.map(chat => chat.id))); } }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [active, reviewOpen, days, archivedOnly, sessionId]);
  const removeSelected = async () => {
    const chats = review!.chats.filter(chat => selected.has(chat.id));
    if (!await showConfirm({ title: t("删除选中的聊天？"), message: t("这将永久删除 {count} 个聊天及其子智能体，无法撤销。", { count: chats.length }), confirmLabel: t("删除"), danger: true })) return;
    setBusy(true); setError("");
    let deleted = 0, skipped = 0, failed = 0;
    for (const chat of chats) {
      try {
        const response = await sendClientCommandAwaitResult({ type: "conversation.delete", conversation_id: chat.id, storage_review: chat.snapshot, client_resource_cleanup: isDesktop() }, "conversation.delete", { silent: true, timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS });
        if (response && commandResultSucceeded(response) && response.data?.deleted === true) deleted += 1;
        else if (response?.data?.reason === "storage_review_changed") skipped += 1;
        else failed += 1;
      } catch { failed += 1; }
    }
    setResult(t("已删除：{deleted}。已变化并跳过：{skipped}。未完成：{failed}。", { deleted, skipped, failed }));
    setBusy(false); setReviewOpen(false); setRevision(value => value + 1);
  };
  return <>
    {error && !reviewOpen && <p role="alert">{error}</p>}{result && <p role="status">{result}</p>}
    <div className="settings-storage-page">
    <section className="settings-group settings-storage-overview"><div className="settings-section-heading"><h3 className="settings-group-title">{t("这台电脑")}</h3><p className="settings-section-description">{t("管理保存在这台电脑上的聊天和工作树。")}</p></div>
      <div className="settings-storage-breakdown">
        <div className="settings-storage-summary"><p id={totalLabelId} className="settings-storage-total">{usage ? t("已使用 {label}", { label: bytes(usage.total_bytes, locale) }) : t("应用数据文件夹")}</p><button type="button" className="settings-action-button" disabled={measuring} aria-busy={measuring} onClick={() => setRevision(value => value + 1)}>{measuring ? t("正在计算存储空间…") : t("刷新")}</button></div>
        {!usage && measuring && <div className="settings-storage-chart settings-storage-chart-loading" role="progressbar" aria-label={t("正在计算存储空间…")} />}
        {usage && categories && <>
          <div className="settings-storage-chart" role="img" aria-labelledby={totalLabelId}>{categories.map(category => <div key={category.name} className="settings-storage-segment" data-category={category.name} style={{ width: `${usage.total_bytes > 0 ? category.bytes / usage.total_bytes * 100 : 0}%` }} />)}</div>
          <ul className="settings-storage-legend">{categories.map(category => <li key={category.name}><span className="settings-storage-category"><span className="settings-storage-dot" data-category={category.name} aria-hidden="true" />{categoryLabels[category.name]}</span><span className="settings-storage-category-bytes">{bytes(category.bytes, locale)}</span></li>)}</ul>
          <p className="settings-storage-path">{usage.path}</p>
        </>}
      </div>
    </section>
    <section className="settings-group"><div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("清理旧聊天")}</div><div className="settings-row-description">{t("永久删除前，先查看聊天及其子智能体。")}</div></div><button type="button" className="settings-action-button" disabled={!sessionId} onClick={() => { setReviewOpen(true); setResult(""); }}>{t("查看")}</button></div>
    </div></section>
    <section className="settings-group"><div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("管理工作树")}</div><div className="settings-row-description">{t("查看工作副本，并选择保留多少个旧工作树。")}</div></div><button type="button" className="settings-action-button" onClick={() => useAppStore.getState().setSettingsTab("worktrees")}>{t("管理")}</button></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("已归档聊天")}</div><div className="settings-row-description">{t("恢复或删除单个已归档聊天。")}</div></div><button type="button" className="settings-action-button" onClick={() => useAppStore.getState().setSettingsTab("archived")}>{t("管理")}</button></div>
    </div></section>
    </div>
    {reviewOpen && active && createPortal(<div className="settings-storage-review-backdrop" onClick={() => { if (!busy) setReviewOpen(false); }}><div ref={reviewRef} role="dialog" aria-modal="true" tabIndex={-1} className="settings-storage-review settings-group" aria-label={t("查看旧聊天")} onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!busy) setReviewOpen(false); } }}><h3 className="settings-group-title">{t("查看旧聊天")}</h3><p className="settings-section-description">{t("大小包含子智能体。删除聊天及其子智能体将永久生效。")}</p>
      <div className="settings-archive-filters"><SelectMenu ariaLabel={t("最后更新")} value={String(days)} onValueChange={value => setDays(Number(value))} disabled={busy}>{[30, 90, 180, 365].map(day => <option key={day} value={day}>{t("超过 {days} 天", { days: day })}</option>)}</SelectMenu><SelectMenu ariaLabel={t("包含")} value={String(archivedOnly)} onValueChange={value => setArchivedOnly(value === "true")} disabled={busy}><option value="true">{t("已归档聊天")}</option><option value="false">{t("所有聊天")}</option></SelectMenu></div>
      {error && <p role="alert">{error}</p>}
      {!review && !error && <p role="status">{t("正在读取聊天…")}</p>}
      {review?.chats.length === 0 && <p>{t("没有符合筛选条件的聊天。")}</p>}
      <div className="settings-card">{review?.chats.slice(0, visible).map(chat => <label key={chat.id} className="settings-row"><input type="checkbox" disabled={busy} checked={selected.has(chat.id)} onChange={() => setSelected(current => { const next = new Set(current); next.has(chat.id) ? next.delete(chat.id) : next.add(chat.id); return next; })} /><span className="settings-row-copy"><span className="settings-row-title">{chat.title}</span><span className="settings-row-description">{new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(new Date(chat.updated_at))}</span></span></label>)}</div>
      {review && visible < review.chats.length && <button type="button" className="settings-action-button" onClick={() => setVisible(value => value + 50)}>{t("显示更多聊天")}</button>}
      <div className="settings-row"><span>{t("已选聊天：{count} · 子智能体：{subagents}", { count: selected.size, subagents: review?.chats.filter(chat => selected.has(chat.id)).reduce((sum, chat) => sum + chat.subagent_count, 0) || 0 })}</span><button type="button" className="settings-action-button" disabled={busy} onClick={() => setReviewOpen(false)}>{t("取消")}</button><button type="button" className="settings-action-button" disabled={busy || selected.size === 0} onClick={() => { void removeSelected(); }}>{busy ? t("正在删除聊天…") : t("删除所选")}</button></div>
    </div></div>, document.body)}
  </>;
}
