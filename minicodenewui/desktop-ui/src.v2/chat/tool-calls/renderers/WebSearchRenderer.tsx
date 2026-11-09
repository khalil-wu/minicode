import { useMemo } from "react";
import { openWebTarget } from "../../openWebTarget";
import { BrandIcon } from "../../../components/BrandIcon";
import "./web-search-renderer.css";

export const WebSearchResultsView = ({ text }: { text: string }) => {
  const items = useMemo(() => {
    const parsedItems: { index: number; title: string; url: string; snippet: string }[] = [];
    const blocks = text.split(/(?:^|\r?\n)(?:\[\d+\]|\d+\.)[ \t]+/);
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
  }, [text]);

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
