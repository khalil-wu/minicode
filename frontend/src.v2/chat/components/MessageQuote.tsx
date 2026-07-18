import * as React from "react";
import { Quote, X } from "lucide-react";
import type { ComposerQuote } from "../../stores/types";
import "./message-quote.css";

interface MessageQuoteProps {
  message: ComposerQuote;
  onRemove: () => void;
}

/**
 * Message quote preview shown above composer
 * Displays the message being replied to
 */
export function MessageQuote({ message, onRemove }: MessageQuoteProps) {
  const compact = message.content.replace(/\s+/g, " ").trim();
  const preview = compact.slice(0, 180);
  const needsEllipsis = compact.length > 180;

  return (
    <div className="message-quote">
      <div className="message-quote-header">
        <Quote size={14} />
        <span className="message-quote-label">
          回复 {message.role === "user" ? "你的消息" : "助手"}
        </span>
        <button
          type="button"
          className="message-quote-remove"
          onClick={onRemove}
          title="取消引用"
          aria-label="取消引用"
        >
          <X size={14} />
        </button>
      </div>
      <div className="message-quote-content">
        {preview}
        {needsEllipsis && "..."}
      </div>
    </div>
  );
}

/**
 * Hook to manage message quote state
 */
export function useMessageQuote() {
  const [quotedMessage, setQuotedMessage] = React.useState<ComposerQuote | null>(null);

  const quoteMessage = (message: ComposerQuote) => {
    setQuotedMessage(message);
  };

  const clearQuote = () => {
    setQuotedMessage(null);
  };

  return {
    quotedMessage,
    quoteMessage,
    clearQuote,
  };
}
