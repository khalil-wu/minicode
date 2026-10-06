import { Quote, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ComposerQuote } from "../../stores/types";
import "./message-quote.css";

interface MessageQuoteProps {
  message: ComposerQuote;
  onRemove?: () => void;
}

/**
 * Message quote preview shown above composer
 * Displays the message being replied to
 */
export function MessageQuote({ message, onRemove }: MessageQuoteProps) {
  return (
    <div className="message-quote">
      <div className="message-quote-header">
        <Quote size={14} />
        <span className="message-quote-label">
          回复 {message.role === "user" ? "你的消息" : "助手"}
        </span>
        {onRemove && <button
          type="button"
          className="message-quote-remove"
          onClick={onRemove}
          title="取消引用"
          aria-label="取消引用"
        >
          <X size={14} />
        </button>}
      </div>
      <div className="message-quote-content">
        <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml unwrapDisallowed
          allowedElements={["p", "strong", "em", "a", "code", "ul", "ol", "li", "blockquote", "br", "del"]}
          components={{
            p: ({ children }) => <span>{children} </span>,
            ul: ({ children }) => <span>{children}</span>,
            ol: ({ children }) => <span>{children}</span>,
            li: ({ children }) => <span> · {children}</span>,
            blockquote: ({ children }) => <span>{children} </span>,
            a: ({ children }) => <span className="message-quote-link">{children}</span>,
          }}>{message.content}</ReactMarkdown>
      </div>
    </div>
  );
}
