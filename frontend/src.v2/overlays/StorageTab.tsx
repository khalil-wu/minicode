import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { commandResultSucceeded, sendClientCommandAwaitResult, LONG_COMMAND_RESULT_TIMEOUT_MS } from "../protocol/ws-outbox";
import { getWebSocket } from "../hooks/useWebSocket";
import { SelectMenu } from "../components/SelectMenu";
import { isDesktop } from "../desktop/runtime";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { Check, LoaderCircle, X } from "../lib/icons";
import { dismissToast, pushToast } from "./ToastContainer";
import "./StorageTab.css";

type CategoryName = "chats" | "archived" | "subagents" | "unclassified" | "worktrees" | "other";
type Usage = { path: string; total_bytes: number; categories: { name: CategoryName; bytes: number }[] };
type ReviewChat = { id: string; title: string; updated_at: string; archived: boolean; subagent_count: number; bytes: number; snapshot: Record<string, unknown> };
type Review = { cutoff: string; days: number; archived_only: boolean; chats: ReviewChat[]; inventory: ReviewChat[] };
const categoryOrder: CategoryName[] = ["chats", "subagents", "archived", "worktrees", "other"];
const bytes = (value: number, locale: string) => {
  if (value < 1024) return new Intl.NumberFormat(locale, { style: "unit", unit: "byte", unitDisplay: "long" }).format(value);
  const units = ["kilobyte", "megabyte", "gigabyte", "terabyte"];
  let size = value / 1024, index = 0;
  for (; size >= 1024 && index < units.length - 1; index += 1) size /= 1024;
  return new Intl.NumberFormat(locale, { style: "unit", unit: units[index], unitDisplay: "short", maximumFractionDigits: 1 }).format(size);
};
const filterReview = (review: Review, days: number, archivedOnly: boolean) => {
  const cutoff = Date.now() - days * 86_400_000;
  return review.inventory.filter(chat => (!archivedOnly || chat.archived) && new Date(chat.updated_at).getTime() < cutoff);
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
  const [confirming, setConfirming] = useState(false);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [reviewError, setReviewError] = useState("");
  const reviewRequest = useRef<AbortController | null>(null);
  const reviewButtonRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [days, setDays] = useState(90);
  const [archivedOnly, setArchivedOnly] = useState(true);
  const [review, setReview] = useState<Review | null>(null);
  const [selected, setSelected] = useState(new Set<string>());
  const [visible, setVisible] = useState(50);
  const [busy, setBusy] = useState(false);
  const reviewRef = useFocusTrap(reviewOpen && active, reviewButtonRef);
  const totalLabelId = useId();
  const reviewTitleId = useId();
  const reviewWarningId = useId();
  const chats = useMemo(() => review ? filterReview(review, days, archivedOnly) : [], [review, days, archivedOnly]);
  useEffect(() => {
    if (reviewOpen && active) cancelRef.current!.focus({ preventScroll: true });
  }, [reviewOpen, active, confirming]);
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
    setReviewOpen(false); setConfirming(false); setReview(null); setReviewLoading(false); setReviewError("");
    return () => reviewRequest.current?.abort();
  }, [active, sessionId]);
  const openReview = async () => {
    const controller = new AbortController();
    reviewRequest.current = controller;
    setReviewLoading(true); setReviewError("");
    const url = new URL("/api/settings/storage/review", apiBase());
    url.searchParams.set("session_id", sessionId!); url.searchParams.set("days", "90"); url.searchParams.set("archived_only", "true");
    try {
      const response = await fetchWithTimeout(url, { headers: authHeaders(), signal: controller.signal });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const next = await response.json() as Review;
      if (controller.signal.aborted) return;
      setReview(next); setDays(90); setArchivedOnly(true); setVisible(50);
      setSelected(new Set(filterReview(next, 90, true).map(chat => chat.id)));
      setConfirming(false); setReviewOpen(true);
    } catch (reason) {
      if (!controller.signal.aborted) setReviewError(String(reason));
    } finally {
      if (!controller.signal.aborted) setReviewLoading(false);
      if (reviewRequest.current === controller) reviewRequest.current = null;
    }
  };
  const changeReviewFilter = (nextDays: number, nextArchivedOnly: boolean) => {
    setDays(nextDays); setArchivedOnly(nextArchivedOnly); setVisible(50);
    setSelected(new Set(filterReview(review!, nextDays, nextArchivedOnly).map(chat => chat.id)));
  };
  const removeSelected = async () => {
    const targets = chats.filter(chat => selected.has(chat.id));
    setBusy(true);
    setReviewOpen(false);
    const pendingToast = pushToast(t("正在删除聊天…"), "info", 0);
    let deleted = 0, skipped = 0, failed = 0;
    for (const chat of targets) {
      try {
        const response = await sendClientCommandAwaitResult({ type: "conversation.delete", conversation_id: chat.id, storage_review: chat.snapshot, client_resource_cleanup: isDesktop() }, "conversation.delete", { silent: true, timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS });
        if (response && commandResultSucceeded(response) && response.data?.deleted === true) deleted += 1;
        else if (response?.data?.reason === "storage_review_changed") skipped += 1;
        else failed += 1;
      } catch { failed += 1; }
    }
    dismissToast(pendingToast);
    pushToast(t("已删除：{deleted}。已变化并跳过：{skipped}。未完成：{failed}。", { deleted, skipped, failed }), failed ? "error" : "success");
    setBusy(false); setRevision(value => value + 1);
  };
  const dismissReview = () => {
    if (busy) return;
    if (confirming) setConfirming(false); else setReviewOpen(false);
  };
  return <>
    {error && <p role="alert">{error}</p>}
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
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("清理旧聊天")}</div><div className="settings-row-description">{t("永久删除前，先查看聊天及其子智能体。")}</div></div><button ref={reviewButtonRef} type="button" className="settings-action-button" disabled={!sessionId || !usage || measuring || Boolean(error) || reviewLoading || busy} aria-busy={reviewLoading} onClick={() => { void openReview(); }}>{reviewLoading && <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />}{t("查看")}</button></div>
    </div>{reviewError && <p role="alert">{reviewError}</p>}</section>
    <section className="settings-group"><div className="settings-card">
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("管理工作树")}</div><div className="settings-row-description">{t("查看工作副本，并选择保留多少个旧工作树。")}</div></div><button type="button" className="settings-action-button" onClick={() => useAppStore.getState().setSettingsTab("worktrees")}>{t("管理")}</button></div>
      <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{t("已归档聊天")}</div><div className="settings-row-description">{t("恢复或删除单个已归档聊天。")}</div></div><button type="button" className="settings-action-button" onClick={() => useAppStore.getState().setSettingsTab("archived")}>{t("管理")}</button></div>
    </div></section>
    </div>
    {reviewOpen && active && createPortal(<div className="settings-storage-review-backdrop" onClick={dismissReview}><div ref={reviewRef} role="dialog" aria-modal="true" tabIndex={-1} className="settings-storage-review" aria-labelledby={reviewTitleId} aria-describedby={reviewWarningId} onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); dismissReview(); } }}>
      {confirming ? <form onSubmit={event => { event.preventDefault(); if (!busy && selected.size > 0) void removeSelected(); }}>
        <header><h2 id={reviewTitleId}>{t("删除选中的聊天？")}</h2></header>
        <div className="settings-storage-review-section"><p id={reviewWarningId} className="settings-section-description">{t("这将永久删除 {count} 个聊天及其子智能体，无法撤销。", { count: selected.size })}</p></div>
        <footer className="settings-storage-review-section settings-storage-review-actions"><button ref={cancelRef} type="button" className="settings-action-button" disabled={busy} onClick={() => setConfirming(false)}>{t("取消")}</button><button type="submit" className="settings-action-button" data-danger="true" disabled={busy}>{t("删除")}</button></footer>
      </form> : <form onSubmit={event => { event.preventDefault(); if (!busy && selected.size > 0) setConfirming(true); }}>
        <header><h2 id={reviewTitleId}>{t("查看旧聊天")}</h2><p id={reviewWarningId}>{t("大小包含子智能体。删除聊天及其子智能体将永久生效。")}</p></header>
        <div className="settings-storage-review-section"><div className="settings-card settings-storage-review-filters">
          <div className="settings-row"><span>{t("最后更新")}</span><SelectMenu className="settings-select" ariaLabel={t("最后更新")} value={String(days)} onValueChange={value => changeReviewFilter(Number(value), archivedOnly)} disabled={busy}>{[30, 90, 180, 365].map(day => <option key={day} value={day}>{t("超过 {days} 天", { days: day })}</option>)}</SelectMenu></div>
          <div className="settings-row"><span>{t("包含")}</span><SelectMenu className="settings-select" ariaLabel={t("包含")} value={String(archivedOnly)} onValueChange={value => changeReviewFilter(days, value === "true")} disabled={busy}><option value="true">{t("已归档聊天")}</option><option value="false">{t("所有聊天")}</option></SelectMenu></div>
        </div></div>
        <div className="settings-storage-review-section settings-storage-review-results">
          {chats.length === 0 ? <p className="settings-storage-review-empty">{t("没有符合筛选条件的聊天。")}</p> : <div className="settings-card">{chats.slice(0, visible).map(chat => <label key={chat.id} className="settings-row settings-storage-review-chat"><span className="settings-storage-review-chat-title">{chat.title}</span><span className="settings-storage-review-chat-control"><span className="settings-storage-review-size">{bytes(chat.bytes, locale)}</span><span className="settings-storage-review-checkbox"><input type="checkbox" disabled={busy} checked={selected.has(chat.id)} aria-label={chat.title} onChange={() => setSelected(current => { const next = new Set(current); next.has(chat.id) ? next.delete(chat.id) : next.add(chat.id); return next; })} /><Check size={12} aria-hidden="true" /></span></span></label>)}</div>}
          {visible < chats.length && <button type="button" className="settings-action-button" onClick={() => setVisible(value => value + 50)}>{t("显示更多聊天")}</button>}
        </div>
        <footer className="settings-storage-review-section"><p aria-live="polite">{t("已选聊天：{count} · 子智能体：{subagents}", { count: selected.size, subagents: chats.filter(chat => selected.has(chat.id)).reduce((sum, chat) => sum + chat.subagent_count, 0) })}</p><div className="settings-storage-review-actions"><button ref={cancelRef} type="button" className="settings-action-button" disabled={busy} onClick={() => setReviewOpen(false)}>{t("取消")}</button><button type="submit" className="settings-action-button" data-danger="true" disabled={busy || selected.size === 0}>{t("删除所选")}</button></div></footer>
      </form>}
      {!confirming && <button type="button" className="settings-storage-review-close" aria-label={t("关闭")} disabled={busy} onClick={() => setReviewOpen(false)}><X size={12} aria-hidden="true" /></button>}
    </div></div>, document.body)}
  </>;
}
