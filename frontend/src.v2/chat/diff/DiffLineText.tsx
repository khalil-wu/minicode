import { Fragment, memo, useMemo } from "react";
import { decorateDiffLineHtml } from "./decorated-diff-syntax";
import type { DiffWordSpan } from "./diff-word-spans";

/** Plain lines update their existing text nodes when the paged reader changes
 * files. Syntax HTML keeps its sanitized token structure and source offsets. */
export const DiffLineText = memo(function DiffLineText({ text, html, spans, textKey }: {
  text: string; html?: string; spans: readonly DiffWordSpan[]; textKey: string;
}) {
  const decorated = useMemo(() => html ? decorateDiffLineHtml(text, html, spans) : undefined, [text, html, spans]);
  if (decorated !== undefined) return <span data-diff-text={textKey} dangerouslySetInnerHTML={{ __html: decorated }} />;
  return <span data-diff-text={textKey}>{spans.length ? <>
    {spans.map((span, index) => <Fragment key={index}>
      {text.slice(index ? spans[index - 1].end : 0, span.start)}
      <span data-diff-span="">{text.slice(span.start, span.end)}</span>
    </Fragment>)}
    {text.slice(spans.at(-1)!.end)}
  </> : text}</span>;
});
