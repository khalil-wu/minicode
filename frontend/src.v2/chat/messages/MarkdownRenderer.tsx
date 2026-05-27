import { Children, memo, useState, useCallback, useMemo, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus, oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import { useAppStore } from "../../stores";
import { pushToast } from "../../overlays/ToastContainer";

interface Props {
  content: string;
  isStreaming?: boolean;
  citations?: {
    source: string;
    range: [number, number];
    label?: string;
    url?: string;
    title?: string;
  }[];
}

type MarkdownNode = {
  type?: string;
  value?: string;
  children?: MarkdownNode[];
};

const fallbackStrongPattern = /\*\*([^*\n]+?)\*\*/g;

const splitFallbackStrongText = (value: string): MarkdownNode[] | null => {
  fallbackStrongPattern.lastIndex = 0;
  const parts: MarkdownNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = fallbackStrongPattern.exec(value)) !== null) {
    if (match.index > lastIndex) {
      parts.push({ type: "text", value: value.slice(lastIndex, match.index) });
    }
    parts.push({
      type: "strong",
      children: [{ type: "text", value: match[1] }],
    });
    lastIndex = match.index + match[0].length;
  }

  if (parts.length === 0) return null;
  if (lastIndex < value.length) {
    parts.push({ type: "text", value: value.slice(lastIndex) });
  }
  return parts;
};

const normalizeFallbackStrongMarkers = () => (tree: MarkdownNode) => {
  const visit = (node: MarkdownNode): void => {
    const children = node.children;
    if (!children) return;

    const nextChildren: MarkdownNode[] = [];
    for (const child of children) {
      if (child.type === "text" && child.value?.includes("**")) {
        const split = splitFallbackStrongText(child.value);
        if (split) {
          nextChildren.push(...split);
          continue;
        }
      }
      visit(child);
      nextChildren.push(child);
    }
    node.children = nextChildren;
  };

  visit(tree);
};

const CopyButton = ({ text }: { text: string }) => {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [text]);

  return (
    <button
      type="button"
      onClick={handleCopy}
      style={codeButtonStyle(copied ? "var(--state-success)" : "var(--text-muted)", 6)}
      className="code-action-btn"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
};

const InsertButton = ({ text }: { text: string }) => {
  const appMode = useAppStore((s) => s.appMode);
  const activeTabPath = useAppStore((s) => s.activeTabPath);
  const insertIntoActiveEditor = useAppStore((s) => s.insertIntoActiveEditor);
  const [inserted, setInserted] = useState(false);
  const canInsert = appMode === "code" && Boolean(activeTabPath);

  const handleInsert = useCallback(() => {
    if (!canInsert) return;
    const event = new CustomEvent("editor:insert-text", { detail: { text, handled: false } });
    window.dispatchEvent(event);
    const ok = event.detail.handled || insertIntoActiveEditor(text);
    if (!ok) {
      pushToast("Open an editable file before inserting code.", "warning", 1800);
      return;
    }
    setInserted(true);
    pushToast(`Inserted into ${activeTabPath}`, "success", 1200);
    window.setTimeout(() => setInserted(false), 1400);
  }, [activeTabPath, canInsert, insertIntoActiveEditor, text]);

  if (!canInsert) return null;
  return (
    <button
      type="button"
      onClick={handleInsert}
      style={codeButtonStyle(inserted ? "var(--state-success)" : "var(--text-muted)", 58)}
      className="code-action-btn"
      title={`Insert into ${activeTabPath}`}
    >
      {inserted ? "Inserted" : "Insert"}
    </button>
  );
};

const codeButtonStyle = (color: string, right: number): React.CSSProperties => ({
  position: "absolute",
  top: 6,
  right,
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "3px 8px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  color,
  opacity: 0,
  transition: "opacity 150ms, color 150ms",
  zIndex: 1,
});

// Hoisted component overrides — stable reference, no re-creation per render
const useResolvedTheme = () => {
  const themeMode = useAppStore((s) => s.themeMode);
  return themeMode === "light"
    ? "light"
    : themeMode === "dark"
      ? "dark"
      : matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark";
};

const mdComponents = (codeStyle: Record<string, React.CSSProperties>, citations: Props["citations"] = []) => ({
  code({ className, children, ...rest }: { className?: string; children?: React.ReactNode; [k: string]: unknown }) {
    const text = String(children).replace(/\n$/, "");
    const match = /language-(\w+)/.exec(className ?? "");
    const nodePos = (rest as { node?: { position?: { start: { line: number }; end: { line: number } } } }).node?.position;
    const isBlock = nodePos && nodePos.end.line > nodePos.start.line;
    if (match || isBlock) {
      return (
        <div className="code-block-wrapper" style={{ position: "relative" }}>
          {match && (
            <div style={{
              display: "flex", justifyContent: "space-between", alignItems: "center",
              padding: "4px 12px", background: "var(--surface-active)",
              borderRadius: "var(--radius-sm, 6px) var(--radius-sm, 6px) 0 0",
              border: "1px solid var(--border-subtle)", borderBottom: "none",
            }}>
              <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                {match[1]}
              </span>
            </div>
          )}
          <InsertButton text={text} />
          <CopyButton text={text} />
          <SyntaxHighlighter
            language={match?.[1] ?? "text"}
            style={codeStyle}
            PreTag="div"
            customStyle={{
              margin: 0, padding: "12px 14px", background: "var(--surface-soft)",
              border: "1px solid var(--border-subtle)",
              borderRadius: match ? "0 0 var(--radius-sm, 6px) var(--radius-sm, 6px)" : "var(--radius-sm, 6px)",
              fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", lineHeight: 1.5,
            }}
          >
            {text}
          </SyntaxHighlighter>
        </div>
      );
    }
    return (
      <code className={className} style={{
        background: "var(--surface-soft)", border: "1px solid var(--border-subtle)",
        borderRadius: 4, padding: "2px 6px", fontFamily: "var(--font-mono)", fontSize: "0.9em",
      }}>
        {children}
      </code>
    );
  },
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
    const href = typeof props.href === "string" ? props.href : "";
    const label = citations.findIndex((citation) => citationHref(citation) === href);
    const childrenText = Children.toArray(props.children).join("");
    const compactChildren = label >= 0 && childrenText === href ? `[${label + 1}]` : props.children;
    return (
      <a {...props} target="_blank" rel="noreferrer" style={{ color: "var(--accent-primary)", textDecoration: "underline" }}>
        {compactChildren}
      </a>
    );
  },
  p: (props: React.HTMLAttributes<HTMLParagraphElement>) => (
    <p {...props} style={{ margin: "8px 0", lineHeight: "var(--leading-relaxed)" }} />
  ),
  ul: (props: React.HTMLAttributes<HTMLUListElement>) => (
    <ul {...props} style={{ margin: "8px 0", paddingLeft: 20 }} />
  ),
  ol: (props: React.HTMLAttributes<HTMLOListElement>) => (
    <ol {...props} style={{ margin: "8px 0", paddingLeft: 20 }} />
  ),
  li: (props: React.HTMLAttributes<HTMLLIElement>) => (
    <li {...props} style={{ marginBottom: 4, lineHeight: "var(--leading-normal)" }} />
  ),
  table: (props: React.HTMLAttributes<HTMLTableElement>) => (
    <div style={{ overflowX: "auto", margin: "8px 0" }}>
      <table {...props} style={{ borderCollapse: "collapse", fontSize: "var(--text-sm)", width: "100%" }} />
    </div>
  ),
  th: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
    <th {...props} style={{ border: "1px solid var(--border-subtle)", padding: "6px 10px", background: "var(--surface-soft)", textAlign: "left", fontWeight: 600 }} />
  ),
  td: (props: React.TdHTMLAttributes<HTMLTableCellElement>) => (
    <td {...props} style={{ border: "1px solid var(--border-subtle)", padding: "6px 10px" }} />
  ),
  blockquote: (props: React.BlockquoteHTMLAttributes<HTMLQuoteElement>) => (
    <blockquote {...props} style={{ borderLeft: "3px solid var(--accent-primary)", background: "var(--surface-soft)", padding: "8px 14px", margin: "8px 0", color: "var(--text-secondary)" }} />
  ),
  h1: (props: React.HTMLAttributes<HTMLHeadingElement>) => <h1 {...props} style={{ fontSize: "var(--text-xl)", fontWeight: 700, margin: "16px 0 8px" }} />,
  h2: (props: React.HTMLAttributes<HTMLHeadingElement>) => <h2 {...props} style={{ fontSize: "var(--text-lg)", fontWeight: 600, margin: "14px 0 6px" }} />,
  h3: (props: React.HTMLAttributes<HTMLHeadingElement>) => <h3 {...props} style={{ fontSize: "var(--text-md)", fontWeight: 600, margin: "12px 0 4px" }} />,
  hr: () => <hr style={{ border: "none", borderTop: "1px solid var(--border-subtle)", margin: "12px 0" }} />,
  img: (props: React.ImgHTMLAttributes<HTMLImageElement>) => (
    <img {...props} style={{ maxWidth: "100%", maxHeight: 480, borderRadius: "var(--radius-sm, 6px)", border: "1px solid var(--border-subtle)", marginBlock: 8, display: "block", objectFit: "contain", cursor: "pointer", background: "var(--surface-soft)" }} loading="lazy"
      onClick={(e) => { const src = (e.target as HTMLImageElement).src; if (src) window.open(src, "_blank"); }} />
  ),
});

const remarkPlugins = [remarkGfm, normalizeFallbackStrongMarkers];

/**
 * Find the split point for incremental rendering during streaming.
 * Returns the index after the last complete block (double newline or closed code fence).
 * Content before this point is "stable" and can be memoized.
 */
function findStableSplitPoint(content: string): number {
  // Look for the last closed code fence followed by a newline
  const fenceClose = content.lastIndexOf("\n```\n");
  // Look for the last double-newline paragraph break
  const paraBreak = content.lastIndexOf("\n\n");
  // Use whichever is later (more content is stable)
  const split = Math.max(
    fenceClose >= 0 ? fenceClose + 5 : -1,
    paraBreak >= 0 ? paraBreak + 2 : -1,
  );
  // Don't split if the stable part is too small (< 100 chars)
  return split > 100 ? split : 0;
}

/** Memoized renderer for the stable (completed) portion of streaming content. */
const StableMarkdown = memo(({ content, components }: { content: string; components: object }) => (
  <ReactMarkdown remarkPlugins={remarkPlugins} components={components as never}>
    {content}
  </ReactMarkdown>
));
StableMarkdown.displayName = "StableMarkdown";

const citationHref = (citation: NonNullable<Props["citations"]>[number] | undefined): string | null => {
  const candidate = citation?.url || citation?.source || "";
  return /^https?:\/\//i.test(candidate) ? candidate : null;
};

const escapeRegex = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const citationListOnlyLinePattern =
  /^\s*(?:来源|参考来源|资料来源|参考|sources?|references?)\s*[:：]\s*(?:\[\d+\](?:\s*<?https?:\/\/\S+>?)?(?:\s*[,;，、]\s*)?)+\s*$/i;

const removeCitationListOnlyLines = (content: string): string =>
  content
    .split(/\r?\n/)
    .filter((line) => !citationListOnlyLinePattern.test(line))
    .join("\n")
    .trim();

const normalizeCitationText = (content: string, citations: Props["citations"] = []): string => {
  if (!citations.length) return content;
  let next = removeCitationListOnlyLines(content);
  citations.forEach((citation, index) => {
    const href = citationHref(citation);
    if (!href) return;
    const n = index + 1;
    const linkedMarker = `[\\[${n}\\]](<${href.replace(/>/g, "%3E")}>)`;
    const escapedHref = escapeRegex(href);

    next = next.replace(new RegExp(`\\[${n}\\]\\s*<?${escapedHref}>?`, "g"), linkedMarker);
    try {
      const parsed = new URL(href);
      const prefix = escapeRegex(`${parsed.origin}${parsed.pathname}`);
      next = next.replace(new RegExp(`\\[${n}\\]\\s*<?${prefix}[^\\s)\\]}>,;]*>?`, "g"), linkedMarker);
    } catch {
      // Exact URL replacement above is enough for non-standard URLs.
    }
    next = next.replace(new RegExp(`\\[${n}\\](?!\\()`, "g"), linkedMarker);
  });
  return next;
};

export const MarkdownRenderer = memo(({ content, isStreaming, citations }: Props) => {
  const resolved = useResolvedTheme();
  const codeStyle = useMemo(() => {
    const base = resolved === "light" ? oneLight : vscDarkPlus;
    return {
      ...base,
      'pre[class*="language-"]': { ...(base['pre[class*="language-"]'] as object), background: "transparent" },
      'code[class*="language-"]': { ...(base['code[class*="language-"]'] as object), background: "transparent" },
    };
  }, [resolved]);
  const components = useMemo(() => mdComponents(codeStyle, citations), [codeStyle, citations]);
  const displayContent = useMemo(() => normalizeCitationText(content, citations), [content, citations]);
  const prevStableRef = useRef("");

  // During streaming, split content into stable prefix + streaming tail
  if (isStreaming && displayContent.length > 200) {
    const splitIdx = findStableSplitPoint(displayContent);
    if (splitIdx > 0) {
      const stableContent = displayContent.slice(0, splitIdx);
      if (stableContent.length > prevStableRef.current.length) {
        prevStableRef.current = stableContent;
      }
      const stable = prevStableRef.current;
      const tail = displayContent.slice(stable.length);
      return (
        <div className="md-body">
          <StableMarkdown content={stable} components={components} />
          {tail && (
            <div style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", lineHeight: "var(--leading-relaxed)" }}>
              {tail}
            </div>
          )}
        </div>
      );
    }
  }

  // Not streaming or content too short — full render
  prevStableRef.current = "";
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components as never}>
        {displayContent}
      </ReactMarkdown>
    </div>
  );
});

MarkdownRenderer.displayName = "MarkdownRenderer";
