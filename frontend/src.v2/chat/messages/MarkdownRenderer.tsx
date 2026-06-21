import { Children, lazy, memo, Suspense, useState, useCallback, useMemo, useRef } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
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
  url?: string;
  children?: MarkdownNode[];
};

type ResolvedTheme = "light" | "dark";
type MarkdownComponents = NonNullable<React.ComponentProps<typeof ReactMarkdown>["components"]>;
type MarkdownRemarkPlugins = NonNullable<React.ComponentProps<typeof ReactMarkdown>["remarkPlugins"]>;
type MarkdownCodeProps = React.HTMLAttributes<HTMLElement> & {
  node?: { position?: { start: { line: number }; end: { line: number } } };
};

type CodeHighlighterProps = {
  children: string;
  hasLanguage: boolean;
  language: string;
  resolvedTheme: ResolvedTheme;
};

const LazyCodeHighlighter = lazy(async () => {
  const [
    highlighterModule,
    darkStyle,
    lightStyle,
    javascript,
    jsx,
    typescript,
    tsx,
    python,
    bash,
    shellSession,
    json,
    diff,
    css,
    markup,
    markdown,
  ] = await Promise.all([
    import("react-syntax-highlighter/dist/esm/prism-light"),
    import("react-syntax-highlighter/dist/esm/styles/prism/vsc-dark-plus"),
    import("react-syntax-highlighter/dist/esm/styles/prism/one-light"),
    import("react-syntax-highlighter/dist/esm/languages/prism/javascript"),
    import("react-syntax-highlighter/dist/esm/languages/prism/jsx"),
    import("react-syntax-highlighter/dist/esm/languages/prism/typescript"),
    import("react-syntax-highlighter/dist/esm/languages/prism/tsx"),
    import("react-syntax-highlighter/dist/esm/languages/prism/python"),
    import("react-syntax-highlighter/dist/esm/languages/prism/bash"),
    import("react-syntax-highlighter/dist/esm/languages/prism/shell-session"),
    import("react-syntax-highlighter/dist/esm/languages/prism/json"),
    import("react-syntax-highlighter/dist/esm/languages/prism/diff"),
    import("react-syntax-highlighter/dist/esm/languages/prism/css"),
    import("react-syntax-highlighter/dist/esm/languages/prism/markup"),
    import("react-syntax-highlighter/dist/esm/languages/prism/markdown"),
  ]);
  const SyntaxHighlighter = highlighterModule.default as React.ComponentType<{
    children: string;
    customStyle: React.CSSProperties;
    language: string;
    PreTag: string;
    showLineNumbers?: boolean;
    style: Record<string, React.CSSProperties>;
    wrapLongLines?: boolean;
  }> & {
    registerLanguage: (name: string, language: unknown) => void;
  };

  SyntaxHighlighter.registerLanguage("javascript", javascript.default);
  SyntaxHighlighter.registerLanguage("jsx", jsx.default);
  SyntaxHighlighter.registerLanguage("typescript", typescript.default);
  SyntaxHighlighter.registerLanguage("tsx", tsx.default);
  SyntaxHighlighter.registerLanguage("python", python.default);
  SyntaxHighlighter.registerLanguage("bash", bash.default);
  SyntaxHighlighter.registerLanguage("shell-session", shellSession.default);
  SyntaxHighlighter.registerLanguage("json", json.default);
  SyntaxHighlighter.registerLanguage("diff", diff.default);
  SyntaxHighlighter.registerLanguage("css", css.default);
  SyntaxHighlighter.registerLanguage("markup", markup.default);
  SyntaxHighlighter.registerLanguage("markdown", markdown.default);

  return {
    default: function CodeHighlighter({
      children,
      hasLanguage,
      language,
      resolvedTheme,
    }: CodeHighlighterProps) {
      const base = resolvedTheme === "light" ? lightStyle.default : darkStyle.default;
      const codeStyle = {
        ...base,
        'pre[class*="language-"]': { ...(base['pre[class*="language-"]'] as object), background: "transparent" },
        'code[class*="language-"]': { ...(base['code[class*="language-"]'] as object), background: "transparent" },
      };
      return (
        <SyntaxHighlighter
          language={normalizeHighlightLanguage(language)}
          style={codeStyle}
          PreTag="div"
          showLineNumbers
          wrapLongLines
          customStyle={codeBlockStyle(hasLanguage)}
        >
          {children}
        </SyntaxHighlighter>
      );
    },
  };
});

const normalizeHighlightLanguage = (language: string): string => {
  const normalized = language.toLowerCase();
  if (normalized === "js") return "javascript";
  if (normalized === "ts") return "typescript";
  if (normalized === "py") return "python";
  if (normalized === "sh" || normalized === "shell" || normalized === "zsh" || normalized === "ps1" || normalized === "powershell") return "bash";
  if (normalized === "html" || normalized === "xml" || normalized === "svg") return "markup";
  if (normalized === "md") return "markdown";
  return normalized;
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

const CODE_FILE_EXTENSIONS = [
  "astro",
  "bash",
  "c",
  "cc",
  "cpp",
  "cs",
  "css",
  "go",
  "h",
  "hpp",
  "html",
  "java",
  "js",
  "jsx",
  "json",
  "kt",
  "md",
  "mdx",
  "php",
  "ps1",
  "py",
  "rb",
  "rs",
  "scss",
  "sh",
  "sql",
  "svelte",
  "toml",
  "ts",
  "tsx",
  "vue",
  "xml",
  "yaml",
  "yml",
].join("|");

const bareFileRefPattern = new RegExp(
  String.raw`(^|[\s([{'"，。；：、])((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[/\\])?(?:[^\s` + "`" + String.raw`"'<>()[\]{}|]+[\\/])*[^\s` + "`" + String.raw`"'<>()[\]{}|]+\.(?:${CODE_FILE_EXTENSIONS})):(\d+)(?::(\d+))?`,
  "gi",
);

const editorLinkUrl = (path: string, line: string, column?: string): string => {
  const params = new URLSearchParams({ path, line });
  if (column) params.set("column", column);
  return `minicode-file-ref:${params.toString()}`;
};

const isWorkspaceRelativeEditorPath = (path: string): boolean => {
  const trimmed = path.trim();
  if (!trimmed || trimmed.includes("\0")) return false;
  if (trimmed.startsWith("/") || trimmed.startsWith("\\") || trimmed.startsWith("~")) return false;
  if (/^[A-Za-z]:[/\\]/.test(trimmed) || /^file:/i.test(trimmed)) return false;
  return !trimmed.split(/[\\/]+/).some((part) => part === "..");
};

const splitBareFileRefs = (value: string): MarkdownNode[] | null => {
  bareFileRefPattern.lastIndex = 0;
  const parts: MarkdownNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = bareFileRefPattern.exec(value)) !== null) {
    const prefix = match[1] ?? "";
    const path = match[2] ?? "";
    const line = match[3] ?? "";
    const column = match[4];
    const fullRef = `${path}:${line}${column ? `:${column}` : ""}`;
    const refStart = match.index + prefix.length;
    if (refStart > lastIndex) {
      parts.push({ type: "text", value: value.slice(lastIndex, refStart) });
    }
    parts.push({
      type: "link",
      url: editorLinkUrl(path, line, column),
      children: [{ type: "text", value: fullRef }],
    });
    lastIndex = refStart + fullRef.length;
  }

  if (parts.length === 0) return null;
  if (lastIndex < value.length) parts.push({ type: "text", value: value.slice(lastIndex) });
  return parts;
};

const linkifyBareFileReferences = () => (tree: MarkdownNode) => {
  const visit = (node: MarkdownNode): void => {
    const children = node.children;
    if (!children || node.type === "link" || node.type === "code" || node.type === "inlineCode") return;

    const nextChildren: MarkdownNode[] = [];
    for (const child of children) {
      if (child.type === "text" && child.value) {
        const split = splitBareFileRefs(child.value);
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
      style={{
        color: copied ? "var(--state-success)" : "var(--text-muted)",
        right: 6,
      }}
      className="code-action-btn absolute top-1.5 bg-[var(--surface-raised)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,4px)] px-2 py-0.5 cursor-pointer text-[var(--text-xs)] opacity-0 transition-[opacity,color] duration-150 z-[1]"
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
      style={{
        color: inserted ? "var(--state-success)" : "var(--text-muted)",
        right: 58,
      }}
      className="code-action-btn absolute top-1.5 bg-[var(--surface-raised)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,4px)] px-2 py-0.5 cursor-pointer text-[var(--text-xs)] opacity-0 transition-[opacity,color] duration-150 z-[1]"
      title={`Insert into ${activeTabPath}`}
    >
      {inserted ? "Inserted" : "Insert"}
    </button>
  );
};

const codeBlockStyle = (hasLanguage: boolean): React.CSSProperties => ({
  margin: 0,
  padding: "12px 14px",
  background: "var(--surface-soft)",
  border: "1px solid var(--border-subtle)",
  borderRadius: hasLanguage ? "0 0 var(--radius-sm, 6px) var(--radius-sm, 6px)" : "var(--radius-sm, 6px)",
  fontSize: "var(--text-sm)",
  fontFamily: "var(--font-mono)",
  lineHeight: 1.5,
});

const lineNumberColumnStyle: React.CSSProperties = {
  display: "inline-block",
  minWidth: "2.25em",
  paddingRight: "0.8em",
  marginRight: "0.8em",
  textAlign: "right",
  color: "var(--text-muted)",
  borderRight: "1px solid var(--border-subtle)",
  userSelect: "none",
};

const PlainCodeBlock = ({ hasLanguage, text }: { hasLanguage: boolean; text: string }) => {
  const lines = text.split("\n");
  return (
    <pre
      style={{
        borderRadius: hasLanguage ? "0 0 var(--radius-sm, 6px) var(--radius-sm, 6px)" : "var(--radius-sm, 6px)",
      }}
      className="m-0 p-3 bg-[var(--surface-soft)] border border-[var(--border-subtle)] text-[var(--text-sm)] font-[var(--font-mono)] leading-[1.5] overflow-x-auto"
    >
      <code>
        {lines.map((line, index) => (
          <span key={index} className="block">
            <span aria-hidden="true" style={lineNumberColumnStyle}>{index + 1}</span>
            {line || " "}
          </span>
        ))}
      </code>
    </pre>
  );
};

// Hoisted component overrides: stable reference, no re-creation per render.
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

const parsePositiveInt = (value: string | null | undefined): number | undefined => {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
};

const editorTargetFromHref = (href: string): { path: string; line?: number; column?: number } | null => {
  if (href.startsWith("minicode-file-ref:")) {
    const params = new URLSearchParams(href.slice("minicode-file-ref:".length));
    const path = params.get("path")?.trim();
    if (!path) return null;
    if (!isWorkspaceRelativeEditorPath(path)) return null;
    return {
      path,
      line: parsePositiveInt(params.get("line")),
      column: parsePositiveInt(params.get("column")),
    };
  }
  if (href.startsWith("file://")) {
    return null;
  }
  if (/^[A-Za-z]:[/\\]|^\/[^/]/.test(href)) {
    return null;
  }
  return null;
};

const mdComponents = (resolvedTheme: ResolvedTheme, citations: Props["citations"] = []): MarkdownComponents => ({
  code({ className, children, node }: MarkdownCodeProps) {
    const text = String(children).replace(/\n$/, "");
    const match = /language-(\w+)/.exec(className ?? "");
    const nodePos = node?.position;
    const isBlock = nodePos && nodePos.end.line > nodePos.start.line;
    if (match || isBlock) {
      return (
        <div className="code-block-wrapper relative">
          {match && (
            <div className="flex justify-between items-center px-3 py-1 bg-[var(--surface-active)] rounded-t-[var(--radius-sm,6px)] border border-[var(--border-subtle)] border-b-0">
              <span className="text-[var(--text-xs)] text-[var(--text-muted)] font-[var(--font-mono)]">
                {match[1]}
              </span>
            </div>
          )}
          <InsertButton text={text} />
          <CopyButton text={text} />
          <Suspense fallback={<PlainCodeBlock hasLanguage={Boolean(match)} text={text} />}>
            <LazyCodeHighlighter
              hasLanguage={Boolean(match)}
              language={match?.[1] ?? "text"}
              resolvedTheme={resolvedTheme}
            >
              {text}
            </LazyCodeHighlighter>
          </Suspense>
        </div>
      );
    }
    return (
      <code className={`${className ?? ""} bg-[var(--surface-soft)] rounded-[5px] px-[5px] py-[1.5px] font-[var(--font-mono)] text-[0.86em] text-[var(--text-primary)]`}>
        {children}
      </code>
    );
  },
  a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => {
    const href = typeof props.href === "string" ? props.href : "";
    const label = citations.findIndex((citation) => citationHref(citation) === href);
    const childrenText = Children.toArray(props.children).join("");
    const compactChildren = label >= 0 && childrenText === href ? `[${label + 1}]` : props.children;
    const editorTarget = editorTargetFromHref(href);
    if (href.startsWith("minicode-file-ref:") && !editorTarget) {
      return <span className="font-[var(--font-mono)] text-[0.9em]">{compactChildren}</span>;
    }
    if (editorTarget) {
      return (
        <button type="button" onClick={() => useAppStore.getState().openEditorFile(
          editorTarget.path,
          undefined,
          { line: editorTarget.line, column: editorTarget.column },
        )}
          className="bg-transparent border-0 p-0 cursor-pointer text-[var(--accent-primary)] underline font-[var(--font-mono)] text-[0.9em]">
          {compactChildren}
        </button>
      );
    }
    return (
      <a {...props} target="_blank" rel="noreferrer" className="text-[var(--accent-primary)] underline">
        {compactChildren}
      </a>
    );
  },
  p: (props: React.HTMLAttributes<HTMLParagraphElement>) => (
    <p {...props} className="my-2 leading-[var(--leading-relaxed)]" />
  ),
  ul: ({ className, ...props }: React.HTMLAttributes<HTMLUListElement>) => (
    <ul {...props} className={`my-2 pl-5 list-disc marker:text-[var(--text-muted)]${className ? ` ${className}` : ""}`} />
  ),
  ol: ({ className, ...props }: React.HTMLAttributes<HTMLOListElement>) => (
    <ol {...props} className={`my-2 pl-5 list-decimal marker:text-[var(--text-muted)]${className ? ` ${className}` : ""}`} />
  ),
  li: ({ className, ...props }: React.HTMLAttributes<HTMLLIElement>) => (
    <li {...props} className={`mb-1 leading-[var(--leading-normal)]${className ? ` ${className}` : ""}`} />
  ),
  table: (props: React.HTMLAttributes<HTMLTableElement>) => (
    <div className="overflow-x-auto my-2">
      <table {...props} className="border-collapse text-[var(--text-sm)] w-full" />
    </div>
  ),
  th: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
    <th {...props} className="border border-[var(--border-subtle)] px-2.5 py-1.5 bg-[var(--surface-soft)] text-left font-semibold" />
  ),
  td: (props: React.TdHTMLAttributes<HTMLTableCellElement>) => (
    <td {...props} className="border border-[var(--border-subtle)] px-2.5 py-1.5" />
  ),
  blockquote: (props: React.BlockquoteHTMLAttributes<HTMLQuoteElement>) => (
    <blockquote {...props} className="border-l-[3px] border-[var(--accent-primary)] bg-[var(--surface-soft)] px-3.5 py-2 my-2 text-[var(--text-secondary)]" />
  ),
  h1: (props: React.HTMLAttributes<HTMLHeadingElement>) => <h1 {...props} className="text-[length:var(--text-2xl)] font-bold mt-5 mb-2.5 first:mt-0" />,
  h2: (props: React.HTMLAttributes<HTMLHeadingElement>) => <h2 {...props} className="text-[length:var(--text-xl)] font-semibold mt-5 mb-2 first:mt-0" />,
  h3: (props: React.HTMLAttributes<HTMLHeadingElement>) => <h3 {...props} className="text-[length:var(--text-lg)] font-semibold mt-4 mb-1.5 first:mt-0" />,
  hr: () => <hr className="border-0 border-t border-[var(--border-subtle)] my-3" />,
  img: (props: React.ImgHTMLAttributes<HTMLImageElement>) => (
    <img {...props} className="max-w-full max-h-[480px] rounded-[var(--radius-sm,6px)] border border-[var(--border-subtle)] my-2 block object-contain cursor-pointer bg-[var(--surface-soft)]" loading="lazy"
      onClick={(e) => { const src = (e.target as HTMLImageElement).src; if (src) window.open(src, "_blank"); }} />
  ),
});

const remarkPlugins: MarkdownRemarkPlugins = [
  [remarkGfm, { singleTilde: false }],
  normalizeFallbackStrongMarkers,
  linkifyBareFileReferences,
];

const markdownUrlTransform = (url: string) => (
  url.startsWith("minicode-file-ref:") ? url : defaultUrlTransform(url)
);

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
const StableMarkdown = memo(({ content, components }: { content: string; components: MarkdownComponents }) => (
  <ReactMarkdown remarkPlugins={remarkPlugins} components={components} urlTransform={markdownUrlTransform}>
    {content}
  </ReactMarkdown>
));
StableMarkdown.displayName = "StableMarkdown";

const citationHref = (citation: NonNullable<Props["citations"]>[number] | undefined): string | null => {
  const candidate = citation?.url || citation?.source || "";
  return /^https?:\/\//i.test(candidate) ? candidate : null;
};

const escapeRegex = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const sourceLabelPattern = "(?:\u6765\u6e90|\u6570\u636e\u6765\u6e90|\u4fe1\u606f\u6765\u6e90|\u53c2\u8003\u6765\u6e90|\u8d44\u6599\u6765\u6e90|\u53c2\u8003|sources?|references?)";
const sourceHeadingLabelPattern = "(?:\u6765\u6e90|\u6570\u636e\u6765\u6e90|\u4fe1\u606f\u6765\u6e90|\u53c2\u8003\u6765\u6e90|\u8d44\u6599\u6765\u6e90|\u53c2\u8003\u6765\u6e90\u5217\u8868|\u53c2\u8003\u8d44\u6599|\u53c2\u8003\u6587\u732e|\u53c2\u8003|sources?|references?)";
const urlOrHostPattern = "(?:<?https?:\\/\\/\\S+>?|[\\w.-]+\\.[a-z]{2,}(?:\\/\\S*)?)";

const sourceLinePattern = new RegExp(
  `^\\s*${sourceLabelPattern}\\s*[:\\uFF1A]\\s*(?:\\[\\d+\\]\\s*)?.*${urlOrHostPattern}.*$`,
  "i",
);
const sourceListOnlyPattern = new RegExp(
  `^\\s*${sourceLabelPattern}\\s*[:\\uFF1A]\\s*(?:\\[\\d+\\](?:\\s*${urlOrHostPattern})?(?:\\s*[,;\\uFF0C\\u3001]\\s*)?)+\\s*$`,
  "i",
);
const sourceCitationSummaryPattern = new RegExp(
  `^\\s*${sourceLabelPattern}\\s*[:\\uFF1A]\\s*(?:\\[\\d+\\]\\s*[^\\[]+)+\\s*$`,
  "i",
);
const sourceHeadingPattern = new RegExp(
  `^\\s{0,3}(?:#{1,6}\\s*)?${sourceHeadingLabelPattern}\\s*[:\\uFF1A]?\\s*$`,
  "i",
);
const sourceItemPattern = new RegExp(
  `^\\s*(?:[-*]\\s*)?(?:\\[\\d+\\]|\\d+[.)])\\s*(?:[^\\n:\\uFF1A]{0,160}(?:[:\\uFF1A]|\\s+)\\s*)?${urlOrHostPattern}.*$`,
  "i",
);
const sourceTitlePattern = new RegExp("^\\s*(?:[-*]\\s*)?(?:\\[\\d+\\]|\\d+[.)])\\s+.+[:\\uFF1A]\\s*$");
const bareUrlPattern = /^\s*<?https?:\/\/\S+>?\s*$/i;
const indexedCitationMarkerPattern = /(?:\[\d+\]|\[\[\\?\d+\\?\]\]\([^)]+\)|\[\\\[\d+\\\]\]\(<[^)]+>\))/g;
const urlOrHostGlobalPattern = new RegExp(urlOrHostPattern, "gi");
const inlineSourceLinkPattern = /\s*\[\s*(?:https?:\/\/[^\]\s]+|(?:www\.)?[\w.-]+\.[a-z]{2,}(?:\/[^\]\s]*)?)\s*\]\(\s*<?https?:\/\/[^)\s>]+>?\s*\)/gi;

const isInlineIndexedSourceList = (line: string): boolean => {
  const trimmed = line.trim();
  if (!trimmed.startsWith("[")) return false;
  const markers = [...trimmed.matchAll(indexedCitationMarkerPattern)];
  if (markers.length < 2 || markers[0].index !== 0) return false;
  return [...trimmed.matchAll(urlOrHostGlobalPattern)].length >= 2;
};

const stripInlineSourceLinks = (line: string): string => (
  line
    .replace(inlineSourceLinkPattern, "")
    .replace(/\s+([。！？!?；;，,])/g, "$1")
);

export const stripModelAuthoredSources = (content: string): string => {
  const lines = content.split(/\r?\n/);
  const kept: string[] = [];
  let inSourceSection = false;
  let inFence = false;

  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) {
      inFence = !inFence;
      kept.push(line);
      continue;
    }
    if (inFence) {
      kept.push(line);
      continue;
    }
    if (
      sourceLinePattern.test(line) ||
      sourceListOnlyPattern.test(line) ||
      sourceCitationSummaryPattern.test(line) ||
      isInlineIndexedSourceList(line)
    ) {
      continue;
    }
    if (sourceHeadingPattern.test(line)) {
      inSourceSection = true;
      continue;
    }
    if (inSourceSection) {
      if (
        line.trim() === "" ||
        sourceItemPattern.test(line) ||
        sourceTitlePattern.test(line) ||
        bareUrlPattern.test(line)
      ) {
        continue;
      }
      inSourceSection = false;
    }
    kept.push(stripInlineSourceLinks(line));
  }

  return kept.join("\n").trim();
};

const removeInlineCitationMarkers = (content: string, citations: Props["citations"] = []): string => {
  let next = content;
  citations.forEach((citation, index) => {
    const href = citationHref(citation);
    if (!href) return;
    const n = index + 1;
    const escapedHref = escapeRegex(href);
    next = next
      .replace(new RegExp(`\\[${n}\\]\\(\\s*<?${escapedHref}>?\\s*\\)`, "g"), "")
      .replace(new RegExp(`\\[\\\\?\\[${n}\\\\?\\]\\]\\(\\s*<?${escapedHref}>?\\s*\\)`, "g"), "");
    try {
      const parsed = new URL(href);
      const prefix = escapeRegex(`${parsed.origin}${parsed.pathname}`);
      next = next
        .replace(new RegExp(`\\[${n}\\]\\(\\s*<?${prefix}[^)\\s>]*>?\\s*\\)`, "g"), "")
        .replace(new RegExp(`\\[\\\\?\\[${n}\\\\?\\]\\]\\(\\s*<?${prefix}[^)\\s>]*>?\\s*\\)`, "g"), "");
    } catch {
      // Exact URL replacement above is enough for non-standard URLs.
    }
    next = next.replace(new RegExp(`\\[${n}\\](?!\\()`, "g"), "");
  });
  return next
    .replace(/[ \t]+([，。！？；：、,.!?;:])/g, "$1")
    .replace(/([（(【「『])\s+/g, "$1")
    .replace(/\s+([）)】」』])/g, "$1")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n[ \t]+/g, "\n")
    .trim();
};

export const normalizeCitationText = (content: string, citations: Props["citations"] = []): string => {
  let next = stripModelAuthoredSources(content);
  if (!citations.length) return next;
  return removeInlineCitationMarkers(next, citations);
};

export const MarkdownRenderer = memo(({ content, isStreaming, citations }: Props) => {
  const resolved = useResolvedTheme();
  const components = useMemo(() => mdComponents(resolved, citations), [resolved, citations]);
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
            <div className="whitespace-pre-wrap font-inherit leading-[var(--leading-relaxed)]">
              {tail}
            </div>
          )}
        </div>
      );
    }
  }

  // Not streaming or content too short: full render.
  prevStableRef.current = "";
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components} urlTransform={markdownUrlTransform}>
        {displayContent}
      </ReactMarkdown>
    </div>
  );
});

MarkdownRenderer.displayName = "MarkdownRenderer";
