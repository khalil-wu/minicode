import { useMemo } from "react";
import { openWebInPreview } from "../../openWebInPreview";
import type { ToolRendererProps } from "../toolRendererRegistry";

export const WebSearchResultsView = ({ text, structured }: { text: string; structured?: string }) => {
  const items = useMemo(() => {
    if (structured) {
      try {
        const parsed = JSON.parse(structured) as { title: string; url: string; snippet?: string }[];
        if (Array.isArray(parsed) && parsed.length > 0) {
          return parsed.map((r, i) => ({ index: i + 1, title: r.title, url: r.url, snippet: r.snippet ?? "" }));
        }
      } catch {
        /* fall through */
      }
    }
    try {
      const parsedItems: { index: number; title: string; url: string; snippet: string }[] = [];
      const blocks = text.split(/\[\d+\]\s+/);
      for (let i = 1; i < blocks.length; i++) {
        const block = blocks[i];
        const lines = block.split("\n");
        const title = lines[0].trim();
        let url = "";
        let snippet = "";
        for (const line of lines.slice(1)) {
          const trimmed = line.trim();
          if (trimmed.startsWith("URL: ")) {
            url = trimmed.slice(5).trim();
          } else if (trimmed.startsWith("片段: ") || trimmed.startsWith("摘要: ") || trimmed.startsWith("snippet: ") || trimmed.startsWith("Snippet: ")) {
            snippet = trimmed.replace(/^(?:片段|摘要|snippet):\s*/i, "").trim();
          }
        }
        if (title && url) {
          parsedItems.push({ index: i, title, url, snippet });
        }
      }
      return parsedItems;
    } catch {
      return [];
    }
  }, [text, structured]);

  const openUrl = (url: string) => {
    openWebInPreview(url);
  };

  if (items.length === 0) {
    return <div className="whitespace-pre-wrap">{text}</div>;
  }

  return (
    <div className="grid gap-3 w-full font-[var(--font-ui)] mt-1.5">
      <div className="text-[var(--text-muted)] text-xs font-medium font-mono">
        SEARCH RESULTS ({items.length})
      </div>
      <div className="grid gap-2">
        {items.map((item) => {
          let hostname = "";
          try {
            hostname = new URL(item.url).hostname;
          } catch {
            hostname = "";
          }
          return (
            <div
              key={item.index}
              className="bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded p-2.5 grid gap-1"
            >
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-1.5 min-w-0 flex-1">
                  {hostname && <span style={domainGlyphStyle}>{hostname.slice(0, 1).toUpperCase()}</span>}
                  <button
                    type="button"
                    onClick={() => openUrl(item.url)}
                    className="bg-transparent border-0 p-0 cursor-pointer font-semibold text-sm text-[var(--accent-primary)] text-left leading-tight underline underline-offset-2 overflow-hidden text-ellipsis whitespace-nowrap"
                  >
                    {item.title}
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => openUrl(item.url)}
                  className="text-xs px-1.5 py-0.5 rounded bg-[var(--surface-base)] border border-[var(--border-subtle)] text-[var(--text-secondary)] cursor-pointer"
                >
                  Open in Preview Pane
                </button>
              </div>
              <div className="text-xs text-[var(--text-muted)] font-mono break-all">
                {item.url}
              </div>
              {item.snippet && (
                <div className="text-xs text-[var(--text-secondary)] leading-normal mt-0.5 font-[var(--font-ui)]">
                  {item.snippet}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

export const WebSearchToolRenderer = ({ record, resultSummary = "", rawResultSummary = "" }: ToolRendererProps) => (
  <WebSearchResultsView text={rawResultSummary || resultSummary} structured={record.contentPreview} />
);

const domainGlyphStyle: React.CSSProperties = {
  width: 14,
  height: 14,
  borderRadius: "var(--radius-sm, 3px)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
  background: "color-mix(in oklch, var(--accent-primary) 12%, var(--surface-base))",
  border: "1px solid var(--border-subtle)",
  color: "var(--accent-primary)",
  fontSize: "9px",
  fontWeight: 700,
  fontFamily: "var(--font-ui)",
};
