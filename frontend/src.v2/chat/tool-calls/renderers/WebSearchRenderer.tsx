import { useMemo } from "react";
import { openWebTarget } from "../../openWebTarget";
import { BrandIcon } from "../../../components/BrandIcon";
import type { ToolCallRecord } from "../../../lib/tool-call-reducer";
import { safeJsonParse } from "../../../lib/safe-parse";
import "./web-search-renderer.css";

export const WebSearchResultsView = ({ text, structured }: { text: string; structured?: string }) => {
  const items = useMemo(() => {
    if (structured) {
      try {
        const parsed = safeJsonParse<unknown>(structured, null);
        if (Array.isArray(parsed) && parsed.length > 0) {
          return parsed.map((raw, i) => {
            const r = raw && typeof raw === "object" ? raw as { title?: unknown; url?: unknown; snippet?: unknown } : {};
            return {
              index: i + 1,
              title: typeof r.title === "string" ? r.title : "",
              url: typeof r.url === "string" ? r.url : "",
              snippet: typeof r.snippet === "string" ? r.snippet : "",
            };
          }).filter((r) => r.title && r.url);
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
    openWebTarget(url);
  };

  if (items.length === 0) {
    return <div className="whitespace-pre-wrap">{text}</div>;
  }

  return (
    <div className="web-search-results">
      <div className="web-search-results-heading">
        搜索结果（{items.length}）
      </div>
      <div className="web-search-results-list">
        {items.map((item) => (
            <div key={item.index} className="web-search-result">
              <div className="web-search-result-header">
                <div className="web-search-result-title">
                  <BrandIcon value={`${item.title} ${item.url}`} websiteUrl={item.url} fallback="web" size={14} />
                  <button
                    type="button"
                    onClick={() => openUrl(item.url)}
                    className="web-search-result-link"
                  >
                    {item.title}
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => openUrl(item.url)}
                  className="web-search-result-open"
                >
                  在浏览器中打开
                </button>
              </div>
              <div className="web-search-result-url">
                {item.url}
              </div>
              {item.snippet && (
                <div className="web-search-result-snippet">
                  {item.snippet}
                </div>
              )}
            </div>
        ))}
      </div>
    </div>
  );
};

export const WebSearchToolRenderer = ({ record, resultSummary = "", rawResultSummary = "" }: {
  record: ToolCallRecord;
  resultSummary?: string;
  rawResultSummary?: string;
}) => (
  <WebSearchResultsView text={rawResultSummary || resultSummary} structured={record.contentPreview} />
);
