import { Children, isValidElement, lazy, memo, Suspense, useState, useCallback, useEffect, useId, useMemo, useRef } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { Folder } from "lucide-react";
import { Icon } from "@iconify/react";
import type { IconifyIcon } from "@iconify/types";
import defaultFileIcon from "@iconify-icons/vscode-icons/default-file";
import cssIcon from "@iconify-icons/vscode-icons/file-type-css";
import excelIcon from "@iconify-icons/vscode-icons/file-type-excel";
import htmlIcon from "@iconify-icons/vscode-icons/file-type-html";
import imageIcon from "@iconify-icons/vscode-icons/file-type-image";
import jsIcon from "@iconify-icons/vscode-icons/file-type-js-official";
import jsonIcon from "@iconify-icons/vscode-icons/file-type-json-official";
import markdownIcon from "@iconify-icons/vscode-icons/file-type-markdown";
import pdfIcon from "@iconify-icons/vscode-icons/file-type-pdf2";
import powerpointIcon from "@iconify-icons/vscode-icons/file-type-powerpoint";
import powershellIcon from "@iconify-icons/vscode-icons/file-type-powershell";
import pythonIcon from "@iconify-icons/vscode-icons/file-type-python";
import reactIcon from "@iconify-icons/vscode-icons/file-type-reactjs";
import tsIcon from "@iconify-icons/vscode-icons/file-type-typescript-official";
import wordIcon from "@iconify-icons/vscode-icons/file-type-word";
import "katex/dist/katex.min.css";
import { useAppStore } from "../../stores";
import type { Citation } from "../../stores/types";
import { pushToast } from "../../overlays/ToastContainer";
import { isPreviewableHttpUrl } from "../openWebInPreview";
import { openWebInBrowser } from "../openWebInBrowser";
import { openWebTarget } from "../openWebTarget";
import { openLocalFilePreview, openWorkspaceFilePreview } from "../openAttachmentPreview";
import { normalizeCitationText } from "./citationText";
import { BrandIcon } from "../../components/BrandIcon";
import { workspaceRawResourceUrlWithToken } from "../../protocol/api";
import { isDesktop, openPath, revealPath } from "../../desktop/runtime";
import { useContextMenu } from "../../components/useContextMenu";
import {
  isWindowsLikeWorkspacePath,
  normalizeWorkspacePath,
  workspacePathWithin,
  workspacePathsEqual,
} from "../../lib/workspace-path";
import {
  createMarkdownHeadingIdAssigner,
  decodeMarkdownFragment,
  markdownHeadingSlug,
} from "../../lib/markdown";

interface Props {
  content: string;
  isStreaming?: boolean;
  citations?: Citation[];
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
type MarkdownRehypePlugins = NonNullable<React.ComponentProps<typeof ReactMarkdown>["rehypePlugins"]>;
type MarkdownNodePosition = { position?: { start: { line: number }; end: { line: number } } };
type MarkdownCodeProps = React.HTMLAttributes<HTMLElement> & {
  node?: MarkdownNodePosition;
};
type MarkdownElementProps<T> = T & { node?: unknown };
type MarkdownPositionedProps<T> = T & { node?: MarkdownNodePosition };
type EditorTarget = { path: string; line?: number; column?: number };
type FileTarget = { path: string };
type FolderTarget = { path: string };

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

const GENERIC_FILE_EXTENSIONS = [
  "7z", "avif", "bmp", "csv", "doc", "docx", "gif", "gz", "ico", "jpeg", "jpg",
  "mov", "mp3", "mp4", "odt", "pdf", "png", "ppt", "pptx", "svg", "tar", "tif", "tiff",
  "conf", "ini", "jsonl", "log", "ndjson", "rst", "txt", "tsv", "wav", "webm", "webp",
  "xls", "xlsx", "zip",
].join("|");
const anyFilePathPattern = new RegExp(String.raw`\.(?:${CODE_FILE_EXTENSIONS}|${GENERIC_FILE_EXTENSIONS})$`, "i");
const externalDeliverablePathPattern = /\.(?:7z|aac|avif|bmp|csv|doc|docx|epub|flac|gif|ico|jpe?g|json|m4a|md|mov|mp3|mp4|odp|ods|odt|ogg|pdf|png|ppt|pptx|rtf|tar|tiff?|tsv|txt|wav|webm|webp|xls|xlsx|xml|ya?ml|zip)$/i;

const bareFileRefPattern = new RegExp(
  String.raw`(^|[\s([{'"，。；：、])((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[/\\])?(?:[^\s` + "`" + String.raw`"'<>()[\]{}|:]+[\\/])*[^\s` + "`" + String.raw`"'<>()[\]{}|:]+\.(?:${CODE_FILE_EXTENSIONS}))(?::(\d+)(?::(\d+))?)?(?=$|[\s,，。;；:：)）\]}])`,
  "gi",
);

const editorLinkUrl = (path: string, line: string, column?: string): string => {
  const params = new URLSearchParams({ path, line });
  if (column) params.set("column", column);
  return `minicode-file-ref:${params.toString()}`;
};

const codeFilePathPattern = new RegExp(String.raw`\.(?:${CODE_FILE_EXTENSIONS})(?::\d+(?::\d+)?)?$`, "i");
const editorTargetPattern = new RegExp(String.raw`^(.+\.(?:${CODE_FILE_EXTENSIONS}))(?::(\d+)(?::(\d+))?)?$`, "i");

const normalizeSlashes = (value: string): string => value.replace(/\\/g, "/").replace(/\/+/g, "/");

const isWorkspaceRelativeEditorPath = (path: string): boolean => {
  const trimmed = path.trim();
  if (!trimmed || trimmed.includes("\0")) return false;
  if (trimmed.includes("://") || trimmed.includes(":")) return false;
  if (trimmed.startsWith("/") || trimmed.startsWith("\\") || trimmed.startsWith("~")) return false;
  if (/^[A-Za-z]:[/\\]/.test(trimmed) || /^file:/i.test(trimmed)) return false;
  return !trimmed.split(/[\\/]+/).some((part) => part === "..");
};

const stripFileRefDecorations = (value: string): string => (
  value.trim().replace(/[?#].*$/, "").replace(/[.,，。;；)）\]}]+$/, "")
);

const isCodeFilePath = (path: string): boolean => {
  const clean = stripFileRefDecorations(path);
  return codeFilePathPattern.test(clean);
};

const parseEditorPathTarget = (value: string): { path: string; line?: number; column?: number } | null => {
  const clean = stripFileRefDecorations(value);
  const match = editorTargetPattern.exec(clean);
  if (!match) return null;
  return {
    path: match[1],
    line: parsePositiveInt(match[2]),
    column: parsePositiveInt(match[3]),
  };
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
    const fullRef = line ? `${path}:${line}${column ? `:${column}` : ""}` : path;
    const refStart = match.index + prefix.length;
    if (!isWorkspaceRelativeEditorPath(path) && !/^[A-Za-z]:[/\\]/.test(path)) continue;
    if (refStart > lastIndex) {
      parts.push({ type: "text", value: value.slice(lastIndex, refStart) });
    }
    parts.push({
      type: "link",
      url: line ? editorLinkUrl(path, line, column) : path,
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

const markdownCodeSegmentPattern = /(```[\s\S]*?```|`[^`\n]*`)/g;
const windowsAbsolutePathPattern = /^[A-Za-z]:(?:[\\/]|%5[cC])/;

const isExplicitLocalImageUrl = (url: string): boolean => (
  url.startsWith("file://")
  || windowsAbsolutePathPattern.test(url)
  || url.startsWith("/")
);

const isLocalImageUrl = (url: string): boolean => (
  isExplicitLocalImageUrl(url)
  || isWorkspaceRelativeEditorPath(url)
);

const isInlineImageDataUrl = (url: string): boolean =>
  /^data:image\/(?:png|jpe?g|gif|webp|avif);base64,[a-z0-9+/=\s]+$/i.test(url.trim());

const isSafeSvgReference = (value: string): boolean => {
  const trimmed = value.trim();
  if (!trimmed || trimmed.startsWith("#")) return true;
  try {
    const parsed = new URL(trimmed, window.location.href);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
};

const isSafeMermaidCss = (css: string): boolean => {
  if (!css.trim()) return false;
  if (/@import|@font-face|expression\s*\(|javascript:|behavior\s*:|-moz-binding/i.test(css)) {
    return false;
  }
  const withoutFragmentUrls = css.replace(/url\(\s*(['"]?)#[^)]+\1\s*\)/gi, "");
  return !/url\s*\(/i.test(withoutFragmentUrls);
};

const sanitizeMermaidSvg = (rawSvg: string): string => {
  if (!rawSvg.trim().startsWith("<svg")) return "";
  try {
    const doc = new DOMParser().parseFromString(rawSvg, "image/svg+xml");
    if (doc.querySelector("parsererror")) return "";
    doc.querySelectorAll("script, foreignObject, iframe, object, embed, link, meta").forEach((node) => {
      node.remove();
    });
    doc.querySelectorAll("style").forEach((node) => {
      if (!isSafeMermaidCss(node.textContent || "")) node.remove();
    });
    doc.querySelectorAll("*").forEach((node) => {
      for (const attr of Array.from(node.attributes)) {
        const name = attr.name.toLowerCase();
        const value = attr.value || "";
        if (name.startsWith("on") || name === "style") {
          node.removeAttribute(attr.name);
          continue;
        }
        if ((name === "href" || name === "xlink:href") && !isSafeSvgReference(value)) {
          node.removeAttribute(attr.name);
        }
      }
    });
    return new XMLSerializer().serializeToString(doc.documentElement);
  } catch {
    return "";
  }
};

/**
 * Normalize model-authored math before remark-math sees it.
 *
 * Models commonly emit display math as `$$formula$$` on one line, or split a
 * formula over lines with the closing `$$` after prose. That is valid intent
 * but not valid micromark block-math syntax: remark-math then treats the first
 * delimiter as an opener and consumes the rest of the answer as one broken
 * formula. Pair the delimiters here and put them on their own lines. Unpaired
 * delimiters are escaped while a stream is incomplete, so the visible answer
 * remains text until the pair arrives.
 */
const normalizeDollarMathSegment = (segment: string): string => {
  let output = "";
  let cursor = 0;

  while (cursor < segment.length) {
    const open = segment.indexOf("$$", cursor);
    if (open < 0) {
      output += segment.slice(cursor);
      break;
    }

    output += segment.slice(cursor, open);
    const close = segment.indexOf("$$", open + 2);
    if (close < 0) {
      // Keep an incomplete stream parseable. The next delta will replace this
      // escaped marker with a real paired block once its closing delimiter is
      // present.
      output += "\\$\\$";
      output += segment.slice(open + 2);
      break;
    }

    const body = segment.slice(open + 2, close).trim();
    if (!body) {
      output += "$$$$";
    } else {
      output += `\n\n$$\n${body}\n$$\n\n`;
    }
    cursor = close + 2;
  }

  return output;
};

const normalizeLatexDelimiters = (value: string): string => {
  if (!value || !/[\\$]/.test(value)) return value;
  return value
    .split(markdownCodeSegmentPattern)
    .map((segment) => {
      if (!segment || segment.startsWith("`")) return segment;
      return normalizeDollarMathSegment(segment)
        .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => {
          const trimmed = String(body || "").trim();
          return trimmed ? `\n\n$$\n${trimmed}\n$$\n\n` : "";
        })
        .replace(/\\\(([\s\S]*?)\\\)/g, (_match, body: string) => {
          const trimmed = String(body || "").trim();
          return trimmed ? `$${trimmed}$` : "";
        });
    })
    .join("");
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
      {copied ? "已复制" : "复制"}
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
      pushToast("请先打开可编辑文件，再插入代码。", "warning", 1800);
      return;
    }
    setInserted(true);
    pushToast(`已插入 ${activeTabPath}`, "success", 1200);
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
      title={`插入到 ${activeTabPath}`}
    >
      {inserted ? "已插入" : "插入"}
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

const MermaidBlock = ({ chart, resolvedTheme }: { chart: string; resolvedTheme: ResolvedTheme }) => {
  const reactId = useId();
  const renderId = useMemo(
    () => `md-mermaid-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`,
    [reactId],
  );
  const [svg, setSvg] = useState<string>("");
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setSvg("");
    setError("");

    import("mermaid")
      .then(async (module) => {
        const mermaid = module.default;
      const rootStyle = getComputedStyle(document.documentElement);
      const tokenColor = (name: string, fallbackLight: string, fallbackDark: string) =>
        rootStyle.getPropertyValue(name).trim()
        || (resolvedTheme === "light" ? fallbackLight : fallbackDark);
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: resolvedTheme === "light" ? "default" : "dark",
        flowchart: { htmlLabels: false },
        themeVariables: {
          textColor: tokenColor("--text-primary", "#111827", "#f3f4f6"),
          primaryTextColor: tokenColor("--text-primary", "#111827", "#f3f4f6"),
          lineColor: tokenColor("--text-secondary", "#4b5563", "#d1d5db"),
        },
      });
        const rendered = await mermaid.render(renderId, chart);
        const safeSvg = sanitizeMermaidSvg(rendered.svg);
        if (!cancelled) {
          if (safeSvg) setSvg(safeSvg);
          else setError("无法安全呈现 Mermaid 图表。");
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "无法呈现 Mermaid 图表。");
      });

    return () => {
      cancelled = true;
    };
  }, [chart, renderId, resolvedTheme]);

  if (error) {
    return (
      <div className="space-y-2">
        <div className="text-[var(--text-xs)] text-[var(--state-warning)] px-3 py-2 bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded-[var(--radius-sm,6px)]">
          Mermaid 呈现失败，已显示源代码。
        </div>
        <PlainCodeBlock hasLanguage text={chart} />
      </div>
    );
  }

  if (!svg) {
    return <PlainCodeBlock hasLanguage text={chart} />;
  }

  return (
    <div
      className="md-mermaid overflow-x-auto p-3 bg-[var(--surface-soft)] border border-[var(--border-subtle)] rounded-b-[var(--radius-sm,6px)] [&_svg]:max-w-full [&_svg]:h-auto [&_svg_text]:fill-[var(--text-primary)] [&_svg_text]:opacity-100"
      data-testid="md-mermaid"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
};

// Hoisted component overrides: stable reference, no re-creation per render.
const useResolvedTheme = () => {
  return useAppStore((s) => s.resolvedTheme);
};

const parsePositiveInt = (value: string | null | undefined): number | undefined => {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined;
};

const isLocalFrontendUrl = (url: URL): boolean =>
  url.protocol === "http:" || url.protocol === "https:"
    ? /^(?:127\.0\.0\.1|localhost|\[::1\])$/i.test(url.hostname)
    : false;

const textFromReactNode = (children: React.ReactNode): string => {
  const pieces: string[] = [];
  const visit = (node: React.ReactNode): void => {
    Children.forEach(node, (child) => {
      if (typeof child === "string" || typeof child === "number") {
        pieces.push(String(child));
        return;
      }
      if (isValidElement<{ children?: React.ReactNode }>(child)) {
        visit(child.props.children);
      }
    });
  };
  visit(children);
  return pieces.join("").trim();
};

const normalizePathLexically = (value: string): string => {
  const normalized = normalizeWorkspacePath(value);
  const driveMatch = /^([A-Za-z]:)(?:\/(.*))?$/.exec(normalized);
  const prefix = driveMatch
    ? `${driveMatch[1]}/`
    : normalized.startsWith("//")
      ? "//"
      : normalized.startsWith("/")
        ? "/"
        : "";
  const body = driveMatch
    ? driveMatch[2] ?? ""
    : normalized.slice(prefix.length);
  const segments: string[] = [];
  for (const segment of body.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      if (segments.length > 0 && segments[segments.length - 1] !== "..") {
        segments.pop();
      } else if (!prefix) {
        segments.push(segment);
      }
      continue;
    }
    segments.push(segment);
  }
  return `${prefix}${segments.join("/")}`.replace(/\/+$/, "") || prefix;
};

const pathWithinWorkspace = (path: string, workspaceRoot: string): boolean => {
  const root = normalizePathLexically(workspaceRoot);
  const target = normalizePathLexically(path);
  return workspacePathWithin(target, root);
};

const absoluteWorkspacePath = (path: string, workspaceRoot: string): string | null => {
  const normalized = normalizeSlashes(path).replace(/^\.\/+/, "");
  if (/^[A-Za-z]:\//.test(normalized) || normalized.startsWith("/")) {
    const absolute = normalizePathLexically(normalized);
    return pathWithinWorkspace(absolute, workspaceRoot) ? absolute : null;
  }
  if (!isWorkspaceRelativeEditorPath(normalized)) return null;
  const root = normalizePathLexically(workspaceRoot);
  return root ? `${root}/${normalized}` : null;
};

const workspaceRelativePath = (path: string, workspaceRoot: string): string | null => {
  const root = normalizePathLexically(workspaceRoot);
  const target = normalizePathLexically(path);
  if (!root || !pathWithinWorkspace(target, root)) return null;
  if (workspacePathsEqual(target, root)) return ".";
  return target.slice(root.length).replace(/^\/+/, "");
};

const isWorkspaceRawResourceUrl = (url: string): boolean => {
  try {
    const parsed = new URL(url, window.location.href);
    return isLocalFrontendUrl(parsed)
      && parsed.pathname === "/api/workspace/raw"
      && parsed.searchParams.has("path");
  } catch {
    return false;
  }
};

const workspacePathFromHref = (
  href: string,
  options: { allowLineTarget?: boolean; allowExternalDeliverable?: boolean } = {},
): string | null => {
  const trimmed = href.trim();
  if (!trimmed) return null;
  const workingDirectory = String(useAppStore.getState().workingDirectory || "");
  let candidate = "";
  let fromLocalFrontend = false;
  if (trimmed.startsWith("minicode-local-file:")) {
    try {
      candidate = decodeURIComponent(trimmed.slice("minicode-local-file:".length));
    } catch {
      return null;
    }
  } else if (windowsAbsolutePathPattern.test(trimmed)) {
    candidate = trimmed;
  } else if (trimmed.startsWith("file://")) {
    try {
      const parsed = new URL(trimmed);
      candidate = decodeURIComponent(parsed.pathname).replace(/^\/([A-Za-z]:\/)/, "$1");
    } catch {
      return null;
    }
  } else if (/^https?:\/\//i.test(trimmed)) {
    try {
      const parsed = new URL(trimmed);
      if (!isLocalFrontendUrl(parsed)) return null;
      const decodedPath = decodeURIComponent(parsed.pathname).replace(/^\/([A-Za-z]:\/)/, "$1");
      candidate = decodedPath;
      fromLocalFrontend = true;
    } catch {
      return null;
    }
  } else if (trimmed.startsWith("/") && !trimmed.startsWith("//")) {
    candidate = trimmed.replace(/^\/([A-Za-z]:[\/\\])/, "$1");
    fromLocalFrontend = true;
  } else {
    candidate = trimmed;
  }

  candidate = normalizeSlashes(stripFileRefDecorations(candidate).replace(/%5[cC]/g, "/")).replace(/^\.\/+/, "");
  if (
    fromLocalFrontend
    && candidate.startsWith("/")
    && !/^\/[A-Za-z]:\//.test(candidate)
    && isWindowsLikeWorkspacePath(workingDirectory)
  ) {
    candidate = candidate.replace(/^\/+/, "");
  }
  const safetyPath = options.allowLineTarget
    ? parseEditorPathTarget(candidate)?.path ?? candidate
    : candidate;
  if (/^[A-Za-z]:\//.test(safetyPath) || safetyPath.startsWith("/")) {
    return pathWithinWorkspace(safetyPath, workingDirectory)
      || (options.allowExternalDeliverable && externalDeliverablePathPattern.test(safetyPath))
      ? candidate
      : null;
  }
  if (!isWorkspaceRelativeEditorPath(safetyPath)) return null;
  return candidate;
};

const localImageWithinWorkspace = (url: string): { path: string; src: string } | null => {
  if (isWorkspaceRawResourceUrl(url)) {
    try {
      const parsed = new URL(url, window.location.href);
      const path = workspacePathFromHref(parsed.searchParams.get("path") || "");
      return path ? { path, src: url } : null;
    } catch {
      return null;
    }
  }
  if (!isLocalImageUrl(url)) return null;
  const workingDirectory = String(useAppStore.getState().workingDirectory || "");
  const candidate = workspacePathFromHref(url);
  if (!candidate) return null;
  const absolutePath = absoluteWorkspacePath(candidate, workingDirectory);
  if (!absolutePath) return null;
  const relativePath = workspaceRelativePath(absolutePath, workingDirectory);
  return relativePath
    ? { path: relativePath, src: workspaceRawResourceUrlWithToken(relativePath, workingDirectory) }
    : null;
};

const workspaceFileTargetFromHref = (href: string): { path: string; line?: number; column?: number } | null => {
  const candidate = workspacePathFromHref(href, { allowLineTarget: true });
  if (!candidate) return null;
  return parseEditorPathTarget(candidate);
};

const workspaceGenericFileTargetFromHref = (href: string): FileTarget | null => {
  const candidate = workspacePathFromHref(href, { allowExternalDeliverable: true });
  if (!candidate || isCodeFilePath(candidate) || !anyFilePathPattern.test(candidate)) return null;
  return { path: candidate };
};

const workspaceFolderTargetFromHref = (href: string): FolderTarget | null => {
  const candidate = workspacePathFromHref(href);
  if (!candidate || anyFilePathPattern.test(candidate)) return null;
  return { path: candidate.replace(/[\\/]+$/, "") };
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
  const workspaceTarget = workspaceFileTargetFromHref(href);
  if (workspaceTarget) return workspaceTarget;
  if (href.startsWith("file://")) {
    return null;
  }
  if (/^[A-Za-z]:[/\\]|^\/[^/]/.test(href)) {
    return null;
  }
  return null;
};

const canUseLinkTextAsEditorTarget = (href: string): boolean => {
  const trimmed = href.trim();
  if (!trimmed) return true;
  if (trimmed.startsWith("minicode-file-ref:")) return true;
  if (/^[A-Za-z]:[/\\]/.test(trimmed)) return true;
  if (trimmed.startsWith("/") || trimmed.startsWith("./") || trimmed.startsWith("../")) return true;
  if (!/^[a-z][a-z\d+.-]*:/i.test(trimmed)) return true;
  try {
    const parsed = new URL(trimmed);
    return parsed.protocol === "file:" || isLocalFrontendUrl(parsed);
  } catch {
    return false;
  }
};

const editorTargetFromLinkText = (href: string, text: string): EditorTarget | null => {
  if (!canUseLinkTextAsEditorTarget(href)) return null;
  return workspaceFileTargetFromHref(text);
};

const proseOptionListPattern = /^\s*[\p{L}\p{N}][\p{L}\p{N}\s&+.-]*(?:\s*\/\s*[\p{L}\p{N}][\p{L}\p{N}\s&+.-]*){1,}\s*$/u;
const proseInlinePattern = /^[\p{L}\p{N}\s·,，.。:：;；!?！？'"“”‘’\-–—\/+&%℃°]+$/u;
const codeLikeInlinePattern = /(?:[`\\{}[\]()<>=]|[_$]|&&|\|\||::|=>|->|[A-Za-z]:[\\/]|\.{1,2}[\\/]|(?:^|\s)[\\/][\w.-]+|[\w.-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|json|md|css|scss|html|tsx?|ya?ml|toml|rs|go|java|kt|swift|php|rb|sh|ps1)\b|(?:^|\s)-{1,2}[\w-]+|^\s*(?:npm|pnpm|yarn|bun|node|npx|python|py|pip|uv|pytest|git|curl|docker|kubectl|powershell|cmd|rg)\b|\b(?:const|let|var|function|return|import|export|class|async|await|def|lambda|SELECT|UPDATE|INSERT|DELETE)\b|[a-z]+[A-Z][A-Za-z]*|\w+\.\w+\()/;

const isProseOptionList = (value: string): boolean => {
  const text = value.trim();
  if (!text || !text.includes("/")) return false;
  if (text.length > 80 || codeLikeInlinePattern.test(text)) return false;
  return proseOptionListPattern.test(text);
};

const shouldRenderInlineCodeAsProse = (value: string): boolean => {
  const text = value.trim();
  if (!text || text.length > 96) return false;
  if (codeLikeInlinePattern.test(text)) return false;
  return proseInlinePattern.test(text);
};

const InlineOptionList = ({ text }: { text: string }) => {
  const parts = text.split("/").map((part) => part.trim()).filter(Boolean);
  if (parts.length < 2) return <>{text}</>;
  return (
    <span className="md-inline-option-list">
      {parts.map((part, index) => (
        <span key={`${part}-${index}`} className="md-inline-option-fragment">
          {index > 0 && <span className="md-inline-option-separator" aria-hidden="true">/</span>}
          <span className="md-inline-option">{part}</span>
        </span>
      ))}
    </span>
  );
};

const fileChipClassName = [
  "md-file-chip",
  "inline-flex max-w-full items-center gap-1.5 align-middle",
  "rounded-[5px] border px-[5px] py-[1px]",
  "font-[var(--font-mono)] text-[0.88em] leading-[1.42]",
  "no-underline cursor-pointer",
  "transition-[background,border-color,color,box-shadow] duration-150",
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent-primary)]",
].join(" ");

const fileTypeLabels: Record<string, string> = {
  bash: "SH",
  c: "C",
  cc: "C++",
  cpp: "C++",
  cs: "C#",
  css: "CSS",
  go: "GO",
  h: "H",
  hpp: "H++",
  html: "HTML",
  java: "JAVA",
  js: "JS",
  jsx: "JSX",
  json: "JSON",
  kt: "KT",
  md: "MD",
  mdx: "MDX",
  php: "PHP",
  ps1: "PS",
  py: "PY",
  rb: "RB",
  rs: "RS",
  scss: "SCSS",
  sh: "SH",
  sql: "SQL",
  svelte: "SV",
  toml: "TOML",
  ts: "TS",
  tsx: "TSX",
  vue: "VUE",
  xml: "XML",
  yaml: "YAML",
  yml: "YAML",
};

const fileExtensionFromPath = (value: string): string => {
  const clean = stripFileRefDecorations(value).replace(/[\\/]+$/, "");
  const base = clean.split(/[\\/]/).pop() ?? "";
  const match = /\.([^.:\s]+)(?::\d+(?::\d+)?)?$/i.exec(base);
  return match?.[1]?.toLowerCase() ?? "";
};

const fileTypeLabel = (extension: string): string => {
  if (!extension) return "FILE";
  return fileTypeLabels[extension] ?? extension.slice(0, 4).toUpperCase();
};

const fileIconForExtension = (extension: string): IconifyIcon => {
  if (extension === "pdf") return pdfIcon;
  if (extension === "doc" || extension === "docx") return wordIcon;
  if (extension === "xls" || extension === "xlsx" || extension === "csv") return excelIcon;
  if (extension === "ppt" || extension === "pptx") return powerpointIcon;
  if (extension === "py") return pythonIcon;
  if (extension === "ts") return tsIcon;
  if (extension === "tsx" || extension === "jsx") return reactIcon;
  if (extension === "js") return jsIcon;
  if (extension === "json") return jsonIcon;
  if (extension === "md" || extension === "mdx") return markdownIcon;
  if (extension === "html") return htmlIcon;
  if (extension === "css" || extension === "scss") return cssIcon;
  if (extension === "ps1") return powershellIcon;
  if (["avif", "bmp", "gif", "ico", "jpeg", "jpg", "png", "svg", "tif", "tiff", "webp"].includes(extension)) return imageIcon;
  return defaultFileIcon;
};

const FileTypeIcon = ({ extension }: { extension: string }) => (
  <span className="md-official-file-icon" data-document-type={extension || "file"} aria-hidden="true">
    <Icon icon={fileIconForExtension(extension)} width="18" height="18" />
  </span>
);

const displayPathParts = (value: string): { directory: string; name: string } => {
  const normalized = normalizeSlashes(value).replace(/^\.\/+/g, "");
  const index = normalized.lastIndexOf("/");
  if (index < 0) return { directory: "", name: normalized };
  return {
    directory: normalized.slice(0, index + 1),
    name: normalized.slice(index + 1),
  };
};

const splitFileLabelMeta = (value: string): { fileName: string; meta: string } => {
  const trimmed = value.trim();
  const lineLabel = /^(.+\.[^\s()]+)\s+(\(line\s+\d+(?::\d+)?\))$/i.exec(trimmed);
  if (lineLabel) return { fileName: lineLabel[1], meta: lineLabel[2] };
  const colonLine = /^(.+\.[^:\s]+)(:\d+(?::\d+)?)$/.exec(trimmed);
  if (colonLine) return { fileName: colonLine[1], meta: colonLine[2] };
  return { fileName: value, meta: "" };
};

const absoluteEditorTitlePath = (path: string, workingDirectory: string): string => {
  const normalizedPath = normalizeSlashes(path).replace(/^\.\/+/g, "");
  if (/^[A-Za-z]:\//.test(normalizedPath)) return normalizedPath;
  const root = normalizeSlashes(workingDirectory).replace(/\/+$/, "");
  return root ? `${root}/${normalizedPath.replace(/^\/+/, "")}` : normalizedPath;
};

const FileReferenceChip = ({ target, children }: { target: EditorTarget; children: React.ReactNode }) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const label = textFromReactNode(children) || target.path;
  const { directory, name } = displayPathParts(label);
  const { fileName, meta } = splitFileLabelMeta(name);
  const titlePath = absoluteEditorTitlePath(target.path, workingDirectory || "");
  const extension = fileExtensionFromPath(target.path) || fileExtensionFromPath(label);
  const { onContextMenu, menu } = useContextMenu(() => [
    {
      label: "在编辑器中打开",
      onClick: () => useAppStore.getState().openEditorFile(target.path, undefined, { line: target.line, column: target.column }),
    },
    // OS shell actions have no meaning in browser mode; offering them there
    // produced a menu entry that did nothing at all.
    ...(isDesktop() ? [
      { label: "使用默认应用打开", onClick: () => { void openPath(titlePath); } },
      { label: "在资源管理器中显示", onClick: () => { void revealPath(titlePath); } },
    ] : []),
    { label: "", separator: true },
    { label: "复制路径", onClick: () => { void navigator.clipboard.writeText(titlePath); } },
  ]);

  return (
    <span className="md-file-reference-wrap relative inline" onContextMenu={onContextMenu}>
      <button
        type="button"
        onClick={() => useAppStore.getState().openEditorFile(
          target.path,
          undefined,
          { line: target.line, column: target.column },
        )}
        title={`在编辑器中打开 ${titlePath}`}
        aria-label={label}
        className={fileChipClassName}
        data-ext={extension || "file"}
      >
        <FileTypeIcon extension={extension} />
        <span className="md-file-chip-label">
          {directory ? <span className="md-file-chip-directory">{directory}</span> : null}
          <span className="md-file-chip-name">{fileName}</span>
          {meta ? <span className="md-file-chip-meta">{meta}</span> : null}
        </span>
      </button>
      {menu}
    </span>
  );
};

const GenericFileReferenceChip = ({ target, children }: { target: FileTarget; children: React.ReactNode }) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const label = textFromReactNode(children) || target.path;
  const { directory, name } = displayPathParts(label);
  const titlePath = absoluteEditorTitlePath(target.path, workingDirectory || "");
  const previewInsideWorkspace = pathWithinWorkspace(titlePath, workingDirectory || "");
  const extension = fileExtensionFromPath(target.path) || fileExtensionFromPath(label);
  const { onContextMenu, menu } = useContextMenu(() => [
    {
      label: "在编辑器中打开（仅文本文件）",
      disabled: true,
    },
    // OS shell actions have no meaning in browser mode; offering them there
    // produced a menu entry that did nothing at all.
    ...(isDesktop() ? [
      { label: "使用默认应用打开", onClick: () => { void openPath(titlePath); } },
      { label: "在资源管理器中显示", onClick: () => { void revealPath(titlePath); } },
    ] : []),
    { label: "", separator: true },
    { label: "复制路径", onClick: () => { void navigator.clipboard.writeText(titlePath); } },
  ]);

  return (
    <span className="md-file-reference-wrap relative inline" onContextMenu={onContextMenu}>
      <button
        type="button"
        onClick={() => {
          if (previewInsideWorkspace) {
            openWorkspaceFilePreview({ path: target.path, name, workspaceRoot: workingDirectory });
          } else {
            void openPath(titlePath);
          }
        }}
        title={previewInsideWorkspace ? `预览 ${titlePath}` : `打开 ${titlePath}`}
        aria-label={label}
        className={fileChipClassName}
        data-ext={extension || "file"}
      >
        <FileTypeIcon extension={extension} />
        <span className="md-file-chip-label">
          {directory ? <span className="md-file-chip-directory">{directory}</span> : null}
          <span className="md-file-chip-name">{name}</span>
        </span>
      </button>
      {menu}
    </span>
  );
};

const FolderReferenceChip = ({ target, children }: { target: FolderTarget; children: React.ReactNode }) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const label = textFromReactNode(children) || target.path;
  const { name } = displayPathParts(label.replace(/[\\/]+$/, ""));
  const titlePath = absoluteEditorTitlePath(target.path, workingDirectory || "");

  return (
    <button
      type="button"
      onClick={() => useAppStore.getState().requestFileTreeReveal(target.path, "folder")}
      title={`在文件树中显示 ${titlePath}`}
      aria-label={label}
      className={`${fileChipClassName} md-folder-chip`}
      data-kind="folder"
    >
      <Folder aria-hidden="true" size={14} strokeWidth={1.8} className="md-folder-chip-icon" />
      <span className="md-file-chip-label">
        <span className="md-file-chip-name">{name}</span>
      </span>
    </button>
  );
};

const MarkdownImage = (props: React.ImgHTMLAttributes<HTMLImageElement>) => {
  const [loadedRemoteUrl, setLoadedRemoteUrl] = useState<string | null>(null);
  const rawSrc = typeof props.src === "string" ? props.src : "";
  const alt = typeof props.alt === "string" ? props.alt : "image";
  const workspaceLocalImage = localImageWithinWorkspace(rawSrc);
  const blockedLocalImage = Boolean(rawSrc) && isLocalImageUrl(rawSrc) && !workspaceLocalImage;
  const src = workspaceLocalImage?.src ?? rawSrc;
  const remoteSrc = !workspaceLocalImage && isPreviewableHttpUrl(src) ? src : "";
  const remoteImagePolicy = useAppStore((s) => s.remoteImagePolicy);
  const allowedRemoteImageDomains = useAppStore((s) => s.allowedRemoteImageDomains);
  const allowRemoteImageDomain = useAppStore((s) => s.allowRemoteImageDomain);
  let remoteHostname = "";
  if (remoteSrc) {
    try {
      remoteHostname = new URL(remoteSrc).hostname.toLowerCase();
    } catch {
      remoteHostname = "";
    }
  }
  const remoteDomainAllowed = Boolean(remoteHostname) && allowedRemoteImageDomains.includes(remoteHostname);
  const remoteImageAllowed = remoteImagePolicy === "allow" || remoteDomainAllowed || loadedRemoteUrl === remoteSrc;
  const previewable = Boolean(src) && (isPreviewableHttpUrl(src) || isInlineImageDataUrl(src) || Boolean(workspaceLocalImage));

  if (blockedLocalImage || !src) {
    return (
      <span
        role="img"
        aria-label={alt}
        className="my-2 inline-flex max-w-full items-center rounded-[var(--radius-sm,6px)] border border-[var(--border-subtle)] bg-[var(--surface-soft)] px-3 py-2 text-[var(--text-sm)] text-[var(--text-muted)]"
        title="本地图片位于当前工作区之外"
      >
        本地图片不可用
      </span>
    );
  }

  if (remoteSrc && (remoteImagePolicy === "block" || !remoteImageAllowed)) {
    const hostname = remoteHostname || "远程主机";
    return (
      <span
        className="my-2 inline-flex max-w-full flex-col items-start gap-2 rounded-[var(--radius-sm,6px)] border border-[var(--border-subtle)] bg-[var(--surface-soft)] px-3 py-2 text-[var(--text-sm)] text-[var(--text-muted)]"
        data-remote-image-placeholder={remoteSrc}
      >
        <span>{alt} · 图片来自 {hostname}</span>
        {remoteImagePolicy === "block" ? (
          <span>设置中已禁止加载远程图片。</span>
        ) : (
          <span className="inline-flex flex-wrap gap-2">
            <button type="button" className="btn-secondary" onClick={() => setLoadedRemoteUrl(remoteSrc)}>
              加载图片
            </button>
            <button type="button" className="btn-ghost" onClick={() => allowRemoteImageDomain(hostname)}>
              本任务允许 {hostname}
            </button>
          </span>
        )}
      </span>
    );
  }

  const openPreview = () => {
    if (workspaceLocalImage) {
      openWorkspaceFilePreview({
        path: workspaceLocalImage.path,
        name: alt,
        workspaceRoot: useAppStore.getState().workingDirectory,
      });
      return;
    }
    if (isPreviewableHttpUrl(src)) {
      openWebInBrowser(src);
      return;
    }
    const mediaType = /^data:([^;,]+)/i.exec(src)?.[1] || "image/png";
    let hash = 0;
    for (let index = 0; index < src.length; index += 1) {
      hash = ((hash << 5) - hash + src.charCodeAt(index)) | 0;
    }
    openLocalFilePreview({
      id: `markdown-image-${Math.abs(hash)}`,
      name: alt,
      mediaType,
      url: src,
    });
  };

  // Remote http(s) images open in the right preview panel; local/inline images
  // open in a lightbox. Distinguish the affordance so users don't expect a zoom
  // lightbox when clicking a remote image.
  const opensInPreviewPanel = isPreviewableHttpUrl(src);
  const previewTitle = "在预览面板中打开";

  const image = (
    <img
      {...props}
      src={src}
      className={`max-w-full max-h-[480px] rounded-[var(--radius-sm,6px)] border border-[var(--border-subtle)] my-2 block object-contain bg-[var(--surface-soft)]${previewable ? (opensInPreviewPanel ? " cursor-pointer" : " cursor-zoom-in") : ""}`}
      loading="lazy"
      title={previewable ? previewTitle : props.title}
      onClick={props.onClick}
    />
  );

  return previewable ? (
    <button
      type="button"
      aria-label={`在预览中打开 ${alt}`}
      className="block max-w-full border-0 bg-transparent p-0 text-left"
      onClick={(event) => {
        if (!event.defaultPrevented) openPreview();
      }}
    >
      {image}
    </button>
  ) : image;
};

const mdComponents = (
  resolvedTheme: ResolvedTheme,
  scopeId: string,
  headingId: ReturnType<typeof createMarkdownHeadingIdAssigner>,
): MarkdownComponents => {
  const heading = (level: 1 | 2 | 3) => ({ node, ...props }: MarkdownPositionedProps<React.HTMLAttributes<HTMLHeadingElement>>) => {
    const base = markdownHeadingSlug(textFromReactNode(props.children));
    const id = headingId(base, node?.position?.start?.line);
    const Tag: "h1" | "h2" | "h3" = level === 1 ? "h1" : level === 2 ? "h2" : "h3";
    const className = level === 1
      ? "text-[length:var(--text-2xl)] font-bold mt-5 mb-2.5 first:mt-0"
      : level === 2
        ? "text-[length:var(--text-xl)] font-semibold mt-5 mb-2 first:mt-0"
        : "text-[length:var(--text-lg)] font-semibold mt-4 mb-1.5 first:mt-0";
    return <Tag {...props} id={id} tabIndex={-1} className={className} style={{ scrollMarginTop: 16, ...props.style }} />;
  };
  return ({
  code({ className, children, node }: MarkdownCodeProps) {
    const text = String(children).replace(/\n$/, "");
    const match = /language-(\w+)/.exec(className ?? "");
    const language = match?.[1]?.toLowerCase() ?? "";
    const nodePos = node?.position;
    const isBlock = nodePos && nodePos.end.line > nodePos.start.line;
    if (language === "mermaid") {
      return (
        <div className="code-block-wrapper relative">
          <div className="flex justify-between items-center px-3 py-1 bg-[var(--surface-active)] rounded-t-[var(--radius-sm,6px)] border border-[var(--border-subtle)] border-b-0">
            <span className="text-[var(--text-xs)] text-[var(--text-muted)] font-[var(--font-mono)]">
              mermaid
            </span>
          </div>
          <CopyButton text={text} />
          <MermaidBlock chart={text} resolvedTheme={resolvedTheme} />
        </div>
      );
    }
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
    const inlineEditorTarget = workspaceFileTargetFromHref(text);
    if (inlineEditorTarget) {
      return <FileReferenceChip target={inlineEditorTarget}>{children}</FileReferenceChip>;
    }
    const inlineFileTarget = workspaceGenericFileTargetFromHref(text);
    if (inlineFileTarget) {
      return <GenericFileReferenceChip target={inlineFileTarget}>{children}</GenericFileReferenceChip>;
    }
    if (isProseOptionList(text)) {
      return <InlineOptionList text={text} />;
    }
    if (shouldRenderInlineCodeAsProse(text)) {
      return <span className="md-inline-code-prose">{children}</span>;
    }
    return (
      <code className={`${className ?? ""} bg-[var(--surface-soft)] rounded-[5px] px-[5px] py-[1.5px] font-[var(--font-mono)] text-[0.86em] text-[var(--text-primary)]`}>
        {children}
      </code>
    );
  },
  a: ({ node: _node, ...props }: MarkdownElementProps<React.AnchorHTMLAttributes<HTMLAnchorElement>>) => {
    const href = typeof props.href === "string" ? props.href : "";
    const childrenText = textFromReactNode(props.children);
    if (href.startsWith("#")) {
      const targetId = `${scopeId}-${markdownHeadingSlug(decodeMarkdownFragment(href.slice(1)))}`;
      return (
        <a
          {...props}
          href={`#${targetId}`}
          className="text-[var(--accent-primary)] underline"
          onClick={(event) => {
            event.preventDefault();
            const element = document.getElementById(targetId);
            element?.scrollIntoView({ behavior: "smooth", block: "start" });
            element?.focus({ preventScroll: true });
          }}
        >
          {props.children}
        </a>
      );
    }
    const editorTarget = editorTargetFromHref(href) ?? editorTargetFromLinkText(href, childrenText);
    const fileTarget = editorTarget
      ? null
      : workspaceGenericFileTargetFromHref(href) ?? (
          canUseLinkTextAsEditorTarget(href) ? workspaceGenericFileTargetFromHref(childrenText) : null
        );
    const folderTarget = editorTarget || fileTarget
      ? null
      : workspaceFolderTargetFromHref(href) ?? (
          canUseLinkTextAsEditorTarget(href) ? workspaceFolderTargetFromHref(childrenText) : null
        );
    const opensInApp = isPreviewableHttpUrl(href);
    if (href.startsWith("minicode-file-ref:") && !editorTarget) {
      return <span className="font-[var(--font-mono)] text-[0.9em]">{props.children}</span>;
    }
    if (editorTarget) {
      return <FileReferenceChip target={editorTarget}>{props.children}</FileReferenceChip>;
    }
    if (fileTarget) {
      return <GenericFileReferenceChip target={fileTarget}>{props.children}</GenericFileReferenceChip>;
    }
    if (folderTarget) {
      return <FolderReferenceChip target={folderTarget}>{props.children}</FolderReferenceChip>;
    }
    return (
      <a
        {...props}
        target={opensInApp ? undefined : "_blank"}
        rel="noreferrer"
        className={opensInApp ? "md-web-link" : "text-[var(--accent-primary)] underline"}
        onClick={(event) => {
          props.onClick?.(event);
          if (event.defaultPrevented) return;
          if (openWebTarget(href)) event.preventDefault();
        }}
      >
        {opensInApp && (
          <BrandIcon
            value={`${childrenText} ${href}`}
            websiteUrl={href}
            fallback="web"
            size={14}
            className="md-web-link-icon"
          />
        )}
        {props.children}
      </a>
    );
  },
  p: ({ node: _node, ...props }: MarkdownElementProps<React.HTMLAttributes<HTMLParagraphElement>>) => (
    <p {...props} className="my-2 leading-[var(--leading-relaxed)]" />
  ),
  ul: ({ className, node: _node, ...props }: MarkdownElementProps<React.HTMLAttributes<HTMLUListElement>>) => (
    <ul {...props} className={`my-2 pl-5 list-disc marker:text-[var(--text-muted)]${className ? ` ${className}` : ""}`} />
  ),
  ol: ({ className, node: _node, ...props }: MarkdownElementProps<React.HTMLAttributes<HTMLOListElement>>) => (
    <ol {...props} className={`my-2 pl-5 list-decimal marker:text-[var(--text-muted)]${className ? ` ${className}` : ""}`} />
  ),
  li: ({ className, node: _node, ...props }: MarkdownElementProps<React.HTMLAttributes<HTMLLIElement>>) => (
    <li {...props} className={`mb-1 leading-[var(--leading-normal)]${className ? ` ${className}` : ""}`} />
  ),
  table: ({ node: _node, ...props }: MarkdownElementProps<React.HTMLAttributes<HTMLTableElement>>) => (
    <div className="overflow-x-auto my-2">
      <table {...props} className="border-collapse text-[var(--text-sm)] w-full" />
    </div>
  ),
  th: ({ node: _node, ...props }: MarkdownElementProps<React.ThHTMLAttributes<HTMLTableCellElement>>) => (
    <th {...props} className="border border-[var(--border-subtle)] px-2.5 py-1.5 bg-[var(--surface-soft)] text-left font-semibold" />
  ),
  td: ({ node: _node, ...props }: MarkdownElementProps<React.TdHTMLAttributes<HTMLTableCellElement>>) => (
    <td {...props} className="border border-[var(--border-subtle)] px-2.5 py-1.5" />
  ),
  blockquote: ({ node: _node, ...props }: MarkdownElementProps<React.BlockquoteHTMLAttributes<HTMLQuoteElement>>) => (
    <blockquote {...props} className="border border-[var(--border-subtle)] rounded-[10px] bg-[var(--surface-soft)] px-3.5 py-2.5 my-2 text-[var(--text-secondary)]" />
  ),
  h1: heading(1),
  h2: heading(2),
  h3: heading(3),
  hr: () => <hr className="border-0 h-px my-4 bg-gradient-to-r from-transparent via-[var(--border-subtle)] to-transparent" />,
  img: ({ node: _node, ...props }: MarkdownElementProps<React.ImgHTMLAttributes<HTMLImageElement>>) => {
    return <MarkdownImage {...props} />;
  },
  });
};

const remarkPlugins: MarkdownRemarkPlugins = [
  [remarkGfm, { singleTilde: false }],
  remarkMath,
  normalizeFallbackStrongMarkers,
  linkifyBareFileReferences,
];

const rehypePlugins: MarkdownRehypePlugins = [
  [rehypeKatex, { strict: false, throwOnError: false }],
];

const markdownUrlTransform = (url: string) => (
  url.startsWith("minicode-file-ref:") || url.startsWith("minicode-local-file:") || isInlineImageDataUrl(url) || isExplicitLocalImageUrl(url)
    ? url
    : defaultUrlTransform(url)
);

const preserveWindowsMarkdownFileLinks = (content: string): string => content.replace(
  /(?<!!)\[([^\]\n]+)\]\(\s*([A-Za-z]:\\[^)\n]+)\s*\)/g,
  (_match, label: string, path: string) => `[${label}](minicode-local-file:${encodeURIComponent(path.trim())})`,
);

/**
 * Find the split point for incremental rendering during streaming.
 * Returns the index after the last complete block boundary.
 * Content before this point is "stable" and can be memoized.
 *
 * This mirrors cc's StreamingMarkdown approach: find the last closed
 * block boundary so everything before it is final and only the tail
 * re-parses per delta. We check multiple boundary types:
 * - Closed backtick or tilde code fences
 * - Double-newline paragraph breaks
 * - Completed list items (line starting with dash, bullet, or 1. followed by \n)
 * - Heading boundaries (# ...\n)
 */
interface FenceScanResult {
  unclosedStart: number;
  lastClosedEnd: number;
}

/** Scan block fences without treating inline backticks or tildes as fences. */
function scanFences(content: string): FenceScanResult {
  let open: { marker: "`" | "~"; length: number; start: number } | null = null;
  let lastClosedEnd = -1;
  let lineStart = 0;

  while (lineStart <= content.length) {
    const newline = content.indexOf("\n", lineStart);
    const lineEnd = newline >= 0 ? newline : content.length;
    const rawLine = content.slice(lineStart, lineEnd);
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    const completeLineEnd = newline >= 0 ? newline + 1 : content.length;

    if (open) {
      const close = /^ {0,3}(`+|~+)[\t ]*$/.exec(line);
      if (
        close
        && close[1][0] === open.marker
        && close[1].length >= open.length
      ) {
        open = null;
        lastClosedEnd = completeLineEnd;
      }
    } else {
      const opening = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
      if (opening) {
        open = {
          marker: opening[1][0] as "`" | "~",
          length: opening[1].length,
          start: lineStart,
        };
      }
    }

    if (newline < 0) break;
    lineStart = newline + 1;
  }

  return {
    unclosedStart: open?.start ?? -1,
    lastClosedEnd,
  };
}

/** Return whether a Markdown heading exists outside a fenced code block. */
function hasMarkdownHeading(content: string): boolean {
  let open: { marker: "`" | "~"; length: number } | null = null;
  let lineStart = 0;

  while (lineStart <= content.length) {
    const newline = content.indexOf("\n", lineStart);
    const lineEnd = newline >= 0 ? newline : content.length;
    const line = content.slice(lineStart, lineEnd).replace(/\r$/, "");

    if (open) {
      const close = /^ {0,3}(`+|~+)[\t ]*$/.exec(line);
      if (close && close[1][0] === open.marker && close[1].length >= open.length) {
        open = null;
      }
    } else {
      const opening = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
      if (opening) {
        open = {
          marker: opening[1][0] as "`" | "~",
          length: opening[1].length,
        };
      } else if (/^ {0,3}#{1,6}(?:[\t ]+|$)/.test(line)) {
        return true;
      }
    }

    if (newline < 0) break;
    lineStart = newline + 1;
  }

  return false;
}

export function findStableSplitPoint(content: string): number {
  const fenceScan = scanFences(content);
  const openFenceStart = fenceScan.unclosedStart;
  // Paragraph/list boundaries inside a still-open code fence are not stable
  // Markdown boundaries.  Restrict every candidate below to the content that
  // precedes the opening fence; the entire growing code block then stays in
  // the cheap PlainCodeBlock tail until its closing fence arrives.
  const stableCandidate = openFenceStart >= 0
    ? content.slice(0, openFenceStart)
    : content;
  let bestSplit = -1;

  // A validated closing fence is stable with or without a final newline. If
  // another fence remains open, only retain closes before that opening.
  if (
    fenceScan.lastClosedEnd >= 0
    && fenceScan.lastClosedEnd <= stableCandidate.length
  ) {
    bestSplit = Math.max(bestSplit, fenceScan.lastClosedEnd);
  }

  // Double-newline paragraph break
  const paraBreak = stableCandidate.lastIndexOf("\n\n");
  if (paraBreak >= 0) bestSplit = Math.max(bestSplit, paraBreak + 2);

  // Heading boundary: line starting with # followed by newline
  const headingEnd = stableCandidate.lastIndexOf("\n# ");
  if (headingEnd >= 0) {
    const headingNewline = stableCandidate.indexOf("\n", headingEnd + 3);
    if (headingNewline >= 0) bestSplit = Math.max(bestSplit, headingNewline + 1);
  }

  // Completed list item: - text\n or * text\n or 1. text\n
  const listPattern = /(?:^|\n)(?:[-*]|\d+\.)\s+.+\n/g;
  let lastListMatch: RegExpExecArray | null = null;
  let match: RegExpExecArray | null;
  while ((match = listPattern.exec(stableCandidate)) !== null) {
    lastListMatch = match;
  }
  if (lastListMatch) {
    bestSplit = Math.max(bestSplit, lastListMatch.index + lastListMatch[0].length);
  }

  // Don't split if the stable part is too small (< 100 chars)
  return bestSplit > 100 ? bestSplit : 0;
}

// ── Plain-text fast path ────────────────────────────────────────────
// If content has no markdown syntax markers, skip the full react-markdown
// pipeline and render as a simple <p>. This covers the majority of short
// assistant replies ("Sure, let me help with that.") and saves ~2-3ms of
// parse + render per message. Mirrors cc's hasMarkdownSyntax() check.
const MD_SYNTAX_RE = /[#*`|[>\-_~]|\n\n|^\d+\. |\n\d+\. /;
const FILE_REF_HINT_RE = new RegExp(
  String.raw`\.(?:${CODE_FILE_EXTENSIONS})(?::\d+(?::\d+)?)?(?=$|[\s,，。;；:：)）\]}])`,
  "i",
);
function hasMarkdownSyntax(s: string): boolean {
  return MD_SYNTAX_RE.test(s) || FILE_REF_HINT_RE.test(s);
}

const PlainText = memo(({ content }: { content: string }) => (
  <p className="md-paragraph" style={{ margin: 0 }}>{content}</p>
));
PlainText.displayName = "PlainText";

/** Memoized renderer for the stable (completed) portion of streaming content. */
const StableMarkdown = memo(({ content, components }: { content: string; components: MarkdownComponents }) => {
  // Check if this is plain text — skip react-markdown entirely
  if (!hasMarkdownSyntax(content)) {
    return <PlainText content={content} />;
  }
  return (
    <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={components} urlTransform={markdownUrlTransform}>
      {content}
    </ReactMarkdown>
  );
});
StableMarkdown.displayName = "StableMarkdown";

const StreamingTailMarkdown = memo(({ content, components }: { content: string; components: MarkdownComponents }) => {
  // Plain-text fast path for the streaming tail too — short tails like
  // a few words being typed don't need the full markdown pipeline.
  if (!hasMarkdownSyntax(content)) {
    return <PlainText content={content} />;
  }
  // An unclosed fence means a code block is still streaming. Sending it through
  // react-markdown re-runs the syntax highlighter over the whole block on every
  // token, which is the dominant cost of a long streamed code block. Render the
  // open fence as unhighlighted text until it closes; the closed block then
  // moves into the stable (memoized, highlighted) prefix on the next split.
  const openFence = scanFences(content).unclosedStart;
  if (openFence >= 0) {
    const before = content.slice(0, openFence);
    const fenceBody = content.slice(openFence);
    const newlineIdx = fenceBody.indexOf("\n");
    const openingLine = (newlineIdx >= 0 ? fenceBody.slice(0, newlineIdx) : fenceBody)
      .replace(/\r$/, "");
    const opening = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(openingLine);
    const infoString = opening?.[2].trim() ?? "";
    const codeText = newlineIdx >= 0 ? fenceBody.slice(newlineIdx + 1) : "";
    return (
      <>
        {before.trim() && (
          <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={components} urlTransform={markdownUrlTransform}>
            {before}
          </ReactMarkdown>
        )}
        <PlainCodeBlock hasLanguage={Boolean(infoString)} text={codeText} />
      </>
    );
  }
  return (
    <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={components} urlTransform={markdownUrlTransform}>
      {content}
    </ReactMarkdown>
  );
});
StreamingTailMarkdown.displayName = "StreamingTailMarkdown";

export const MarkdownRenderer = memo(({ content, isStreaming, citations }: Props) => {
  const resolved = useResolvedTheme();
  const rawScopeId = useId();
  const scopeId = useMemo(() => `md-${rawScopeId.replace(/[^a-zA-Z0-9_-]/g, "")}`, [rawScopeId]);
  const headingId = useMemo(() => createMarkdownHeadingIdAssigner(scopeId), [scopeId]);
  const displayContent = useMemo(
    () => preserveWindowsMarkdownFileLinks(normalizeLatexDelimiters(normalizeCitationText(content, citations))),
    [content, citations],
  );
  const components = useMemo(
    () => mdComponents(resolved, scopeId, headingId),
    [resolved, scopeId, headingId],
  );
  headingId.reset();
  const prevStableRef = useRef("");

  // Plain-text fast path: skip react-markdown entirely for content with
  // no markdown syntax. This is the single biggest render-cost win for
  // short replies and process narration text.
  const isPlainText = !hasMarkdownSyntax(displayContent);

  // During streaming, split content into stable prefix + streaming tail
  if (isStreaming && displayContent.length > 200) {
    const splitIdx = findStableSplitPoint(displayContent);
    if (splitIdx > 0) {
      const stableContent = displayContent.slice(0, splitIdx);
      if (!prevStableRef.current || !stableContent.startsWith(prevStableRef.current)) {
        prevStableRef.current = stableContent;
      } else if (stableContent.length > prevStableRef.current.length) {
        prevStableRef.current = stableContent;
      }
      const stable = prevStableRef.current;
      const tail = displayContent.slice(stable.length);
      if (!hasMarkdownHeading(tail)) {
        return (
          <div className="md-body">
            <StableMarkdown content={stable} components={components} />
            {tail && <StreamingTailMarkdown content={tail} components={components} />}
          </div>
        );
      }
    }
  }

  // Not streaming or content too short: full render.
  prevStableRef.current = "";

  // Plain-text fast path: skip the full react-markdown pipeline
  if (isPlainText) {
    return (
      <div className="md-body">
        <PlainText content={displayContent} />
      </div>
    );
  }

  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={components} urlTransform={markdownUrlTransform}>
        {displayContent}
      </ReactMarkdown>
    </div>
  );
});

MarkdownRenderer.displayName = "MarkdownRenderer";
