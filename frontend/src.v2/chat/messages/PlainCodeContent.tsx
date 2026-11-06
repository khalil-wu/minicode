import { memo } from "react";

const LINES_PER_CHUNK = 64;

const CodeLines = memo(({ text, start }: { text: string; start: number }) => <span className="contents">
  {text.split("\n").map((line, index) => <span className="block" key={index}>
    <span aria-hidden="true" className="code-theme-line-number">{start + index + 1}</span>{line}
  </span>)}
</span>);

/** Completed line groups keep their DOM while an open fence keeps growing. */
export const PlainCodeContent = ({ text }: { text: string }) => {
  const lines = text.split("\n");
  const chunks = [];
  for (let start = 0; start < lines.length; start += LINES_PER_CHUNK) {
    chunks.push(<CodeLines key={start} start={start} text={lines.slice(start, start + LINES_PER_CHUNK).join("\n")} />);
  }
  return <>{chunks}</>;
};
