import { Check, Copy } from "lucide-react";
import { memo, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ThemedToken } from "@shikijs/core";
import { resolveLang, tokenize } from "../lib/highlight";
import { copyText, cx } from "../lib/util";

export function Tokens({ tokens }: { tokens: ThemedToken[] }) {
  return (
    <>
      {tokens.map((t, i) => (
        <span key={i} style={t.color ? { color: t.color, fontStyle: t.fontStyle === 1 ? "italic" : undefined } : undefined}>
          {t.content}
        </span>
      ))}
    </>
  );
}

export function CodeBlock({ code, lang, lineNumbers }: { code: string; lang?: string; lineNumbers?: boolean }) {
  const [copied, setCopied] = useState(false);
  const lines = tokenize(code.replace(/\n$/, ""), resolveLang(lang));
  return (
    <div className="codeblock">
      <div className="codeblock-head">
        <span className="codeblock-lang">{lang || "text"}</span>
        <button
          className="codeblock-copy"
          aria-label="复制代码"
          onClick={async () => {
            await copyText(code);
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          }}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className={cx("codeblock-body", lineNumbers && "numbered")}>
        <code>
          {lines.map((line, i) => (
            <span key={i} className="code-line">
              {lineNumbers && <span className="ln">{i + 1}</span>}
              <Tokens tokens={line} />
              {"\n"}
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
}

function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && typeof node === "object" && "props" in node) return textOf((node as { props: { children?: ReactNode } }).props.children);
  return "";
}

export const Markdown = memo(function Markdown({ text, streaming }: { text: string; streaming?: boolean }) {
  return (
    <div className={cx("md", streaming && "streaming")}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          pre: ({ children }) => {
            const child = Array.isArray(children) ? children[0] : children;
            const className = (child as { props?: { className?: string } })?.props?.className ?? "";
            const lang = /language-(\S+)/.exec(className)?.[1];
            return <CodeBlock code={textOf(child)} lang={lang} />;
          },
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className="md-table">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
