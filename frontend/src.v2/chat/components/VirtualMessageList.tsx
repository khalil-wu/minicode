import { useRef, useMemo } from "react";
import { useVirtualScroll } from "../../hooks/useVirtualScroll";
import type { ChatMessage } from "../../stores/types";

interface VirtualMessageListProps {
  messages: ChatMessage[];
  renderMessage: (message: ChatMessage, index: number) => React.ReactNode;
  threshold?: number; // Enable virtualization when message count exceeds this
}

/**
 * Smart virtual message list that only enables virtualization
 * when the message count exceeds the threshold
 */
export function VirtualMessageList({
  messages,
  renderMessage,
  threshold = 50,
}: VirtualMessageListProps) {
  const parentRef = useRef<HTMLDivElement>(null);
  const enableVirtualization = messages.length > threshold;

  const { virtualItems, totalSize } = useVirtualScroll(parentRef, {
    itemCount: messages.length,
    estimateSize: 200, // Average message height
    overscan: 5,
  });

  // Non-virtualized rendering for small lists
  if (!enableVirtualization) {
    return (
      <div ref={parentRef} style={containerStyle}>
        {messages.map((message, index) => (
          <div key={message.id} data-index={index}>
            {renderMessage(message, index)}
          </div>
        ))}
      </div>
    );
  }

  // Virtualized rendering for large lists
  return (
    <div ref={parentRef} style={containerStyle}>
      <div style={{ height: totalSize, position: "relative" }}>
        {virtualItems.map((virtualItem) => {
          const message = messages[virtualItem.index];
          if (!message) return null;

          return (
            <div
              key={message.id}
              data-index={virtualItem.index}
              style={{
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${virtualItem.start}px)`,
              }}
            >
              {renderMessage(message, virtualItem.index)}
            </div>
          );
        })}
      </div>
    </div>
  );
}

const containerStyle: React.CSSProperties = {
  height: "100%",
  overflow: "auto",
  position: "relative",
};
