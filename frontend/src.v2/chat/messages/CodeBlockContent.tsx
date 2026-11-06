import { memo } from "react";
import { useMeasuredMarkdownBlock } from "./useMeasuredMarkdownBlock";

const LINES_PER_CHUNK = 64;

const CodeLines = memo(({ text, html, start }: { text: string; html?: string; start: number }) => {
  const ref = useMeasuredMarkdownBlock<HTMLSpanElement>();
  return html !== undefined
    ? <span ref={ref} className="code-line-group block md-measured-block" dangerouslySetInnerHTML={{ __html: html }} />
    : <span ref={ref} className="code-line-group block md-measured-block">
      {text.split("\n").map((line, index) => <span className="block" key={index}>
        <span aria-hidden="true" className="code-theme-line-number">{start + index + 1}</span>{line}
      </span>)}
    </span>;
});

/** Completed line groups keep their DOM while an open fence keeps growing. */
export const CodeBlockContent = ({ text, highlightedLines }: { text: string; highlightedLines?: readonly string[] }) => {
  const lines = text.split("\n");
  const chunks = [];
  for (let start = 0; start < lines.length; start += LINES_PER_CHUNK) {
    const html = highlightedLines?.slice(start, start + LINES_PER_CHUNK).map((line, index) =>
      `<span class="block"><span aria-hidden="true" class="code-theme-line-number">${start + index + 1}</span>${line}</span>`).join("");
    chunks.push(<CodeLines key={start} start={start} text={lines.slice(start, start + LINES_PER_CHUNK).join("\n")} html={html} />);
  }
  return <>{chunks}</>;
};
