import { memo, useEffect, useLayoutEffect, useState } from "react";
import { flushSync } from "react-dom";
import { useTranscriptSearch } from "../TranscriptSearchContext";
import { useMeasuredMarkdownBlock } from "./useMeasuredMarkdownBlock";

const LINES_PER_CHUNK = 64;

const CodeLines = memo(({ text, html, start, reveal }: { text: string; html?: string; start: number; reveal: boolean }) => {
  const ref = useMeasuredMarkdownBlock<HTMLSpanElement>();
  const [skipped, setSkipped] = useState(false);
  const [visibleHtml, setVisibleHtml] = useState(html);
  useEffect(() => {
    const element = ref.current!;
    const visibilityChanged = (event: Event) => setSkipped((event as ContentVisibilityAutoStateChangeEvent).skipped);
    element.addEventListener("contentvisibilityautostatechange", visibilityChanged);
    return () => element.removeEventListener("contentvisibilityautostatechange", visibilityChanged);
  }, [ref]);
  // The browser already knows which measured groups are offscreen. Keep their
  // literal source available to selection/search without building token DOM.
  // A rendered group retains its colors when it leaves the viewport.
  useLayoutEffect(() => {
    if (visibleHtml === html || skipped && !reveal && html !== undefined) return;
    const element = ref.current!;
    const applyHighlight = () => {
      const selection = element.ownerDocument.getSelection();
      if (html !== undefined && selection && !selection.isCollapsed && selection.getRangeAt(0).intersectsNode(element)) return;
      setVisibleHtml(html);
    };
    applyHighlight();
    element.ownerDocument.addEventListener("selectionchange", applyHighlight);
    return () => element.ownerDocument.removeEventListener("selectionchange", applyHighlight);
  }, [html, visibleHtml, skipped, reveal, ref]);
  return visibleHtml !== undefined
    ? <span ref={ref} className="code-line-group block md-measured-block" dangerouslySetInnerHTML={{ __html: visibleHtml }} />
    : <span ref={ref} className="code-line-group block md-measured-block">
      {text.split("\n").map((line, index) => <span className="block" key={index}>
        <span aria-hidden="true" className="code-theme-line-number">{start + index + 1}</span>{line}
      </span>)}
    </span>;
});

/** Completed line groups keep their DOM while an open fence keeps growing. */
export const CodeBlockContent = ({ text, highlightedLines }: { text: string; highlightedLines?: readonly string[] }) => {
  const searching = useTranscriptSearch();
  const [printing, setPrinting] = useState(false);
  useEffect(() => {
    const media = window.matchMedia("print");
    setPrinting(media.matches);
    const changed = (event: MediaQueryListEvent) => setPrinting(event.matches);
    // Native print captures synchronously after beforeprint returns.
    const beforePrint = () => flushSync(() => setPrinting(true));
    const afterPrint = () => setPrinting(false);
    media.addEventListener("change", changed);
    window.addEventListener("beforeprint", beforePrint);
    window.addEventListener("afterprint", afterPrint);
    return () => {
      media.removeEventListener("change", changed);
      window.removeEventListener("beforeprint", beforePrint);
      window.removeEventListener("afterprint", afterPrint);
    };
  }, []);
  const lines = text.split("\n");
  const chunks = [];
  for (let start = 0; start < lines.length; start += LINES_PER_CHUNK) {
    const html = highlightedLines?.slice(start, start + LINES_PER_CHUNK).map((line, index) =>
      `<span class="block"><span aria-hidden="true" class="code-theme-line-number">${start + index + 1}</span>${line}</span>`).join("");
    chunks.push(<CodeLines key={start} start={start} text={lines.slice(start, start + LINES_PER_CHUNK).join("\n")} html={html} reveal={searching || printing} />);
  }
  return <>{chunks}</>;
};
