import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, FileText, Globe2, Image, Presentation, Sheet } from "lucide-react";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { selectActiveConversationPreview } from "../lib/preview-projection";
import { collectArtifacts } from "../lib/conversation-artifact-items";
import type { ChatMessage } from "../stores/types";
import "./NewTabRecents.css";

export type NewTabRecentArtifact = ReturnType<typeof collectArtifacts>[number];
const noMessages: ChatMessage[] = [];
const copy = {
  "en-US": { list: "Recent work", up: "Scroll recents up", down: "Scroll recents down", document: "Document", image: "Image", pdf: "PDF", presentation: "Presentation", spreadsheet: "Spreadsheet", website: "Website", file: "File" },
  "zh-CN": { list: "最近工作", up: "向上滚动最近工作", down: "向下滚动最近工作", document: "文档", image: "图片", pdf: "PDF", presentation: "演示文稿", spreadsheet: "电子表格", website: "网页", file: "文件" },
};
function fileType(item: NewTabRecentArtifact): keyof Pick<typeof copy["en-US"], "document" | "image" | "pdf" | "presentation" | "spreadsheet" | "website" | "file"> {
  const media = item.mediaType?.toLowerCase() ?? "";
  if (item.kind === "image" || media.startsWith("image/")) return "image";
  if (media === "application/pdf") return "pdf";
  if (media.includes("presentation") || media.includes("powerpoint")) return "presentation";
  if (media.includes("spreadsheet") || media.includes("excel") || media === "text/csv") return "spreadsheet";
  if (media === "text/html") return "website";
  if (media.startsWith("text/") || media.includes("wordprocessing") || media === "application/msword") return "document";
  return "file";
}

/** The launcher consumes existing output projections; opening a tab never fetches history. */
export function NewTabRecents({ conversationId, replaceTabId, onOpen }: {
  conversationId: string;
  replaceTabId: string;
  onOpen: (item: NewTabRecentArtifact, replaceTabId: string, conversationId: string) => void;
}) {
  const { t, locale } = useI18n();
  const labels = copy[locale];
  const messages = useAppStore(state => state.conversationId === conversationId ? state.messages : noMessages);
  const preview = useAppStore(state => state.conversationId === conversationId ? selectActiveConversationPreview(state).previewArtifact : null);
  const items = useMemo(() => collectArtifacts(messages.filter(message => message.role === "assistant"), preview, conversationId)
    .filter(item => item.kind !== "attachment" && !item.executionResult && Boolean(item.artifactId || item.path))
    .sort((left, right) => (right.occurredAt ?? 0) - (left.occurredAt ?? 0)).slice(0, 24), [messages, preview, conversationId]);
  const listRef = useRef<HTMLUListElement>(null);
  const [edges, setEdges] = useState({ top: true, bottom: true });
  const [interacted, setInteracted] = useState(false);
  const updateEdges = useCallback(() => {
    const list = listRef.current!;
    const top = list.scrollTop < 1, bottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 1;
    setEdges(current => current.top === top && current.bottom === bottom ? current : { top, bottom });
  }, []);
  useLayoutEffect(() => {
    if (!conversationId || items.length === 0) return;
    const list = listRef.current!, root = list.closest<HTMLElement>("[data-new-tab-scroll-root]")!;
    const measure = () => {
      const row = list.firstElementChild as HTMLElement;
      const height = row.offsetHeight;
      if (height === 0) return;
      const rootBounds = root.getBoundingClientRect();
      const scale = root.offsetHeight > 0 ? rootBounds.height / root.offsetHeight : 1;
      const top = (list.getBoundingClientRect().top - rootBounds.top) / (scale || 1) + root.scrollTop;
      const padding = parseFloat(getComputedStyle(root).paddingBottom) || 0;
      const rows = Math.max(2, Math.floor((root.clientHeight - top - padding - height / 2) / height));
      list.style.maxHeight = `${Math.min(6, rows + .5) * height}px`;
      list.style.setProperty("--recent-row-height", `${height}px`);
      updateEdges();
    };
    const observer = new ResizeObserver(measure);
    observer.observe(root); observer.observe(root.firstElementChild!); observer.observe(list);
    measure();
    return () => observer.disconnect();
  }, [conversationId, items.length, updateEdges]);
  if (!conversationId || items.length === 0) return null;
  const scroll = (direction: number) => {
    setInteracted(true);
    const list = listRef.current!, row = list.firstElementChild as HTMLElement;
    const height = row.offsetHeight || list.clientHeight;
    const rows = Math.max(1, Math.floor((list.clientHeight - height / 2) / height));
    const preference = document.documentElement.dataset.reducedMotion;
    const reduced = preference === "true" || preference !== "false" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    list.scrollBy({ top: direction * rows * height, behavior: reduced ? "instant" : "smooth" });
  };
  const today = new Date();
  return <section className="mc-new-tab-recents" aria-label={labels.list}>
    <header><h3>{t("最近")}</h3><div><button type="button" aria-label={labels.up} disabled={edges.top} onClick={() => scroll(-1)}><ChevronUp size={16} /></button><button type="button" aria-label={labels.down} disabled={edges.bottom} onClick={() => scroll(1)}><ChevronDown size={16} /></button></div></header>
    <ul ref={listRef} aria-label={labels.list} data-at-bottom={edges.bottom} data-interacted={interacted} onScroll={updateEdges}
      onWheel={() => setInteracted(true)} onTouchStart={() => setInteracted(true)} onKeyDown={() => setInteracted(true)}>
      {items.map(item => {
        const kind = fileType(item), Icon = kind === "image" ? Image : kind === "presentation" ? Presentation : kind === "spreadsheet" ? Sheet : kind === "website" ? Globe2 : FileText;
        const date = typeof item.occurredAt === "number" && Number.isFinite(item.occurredAt) ? new Date(item.occurredAt) : null;
        const sameDay = date?.toDateString() === today.toDateString();
        return <li key={item.id}><button type="button" aria-label={item.label} onClick={() => onOpen(item, replaceTabId, conversationId)}>
          <span className="mc-new-tab-recent-icon"><Icon size={16} aria-hidden="true" /></span><span className="mc-new-tab-recent-copy"><span>{item.label}</span><small>{labels[kind]}</small></span>
          {date && <time dateTime={date.toISOString()} title={new Intl.DateTimeFormat(locale, { dateStyle: "full", timeStyle: "long" }).format(date)}>{sameDay ? new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit" }).format(date) : new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", year: date.getFullYear() === today.getFullYear() ? undefined : "numeric" }).format(date)}</time>}
        </button></li>;
      })}
    </ul>
  </section>;
}
