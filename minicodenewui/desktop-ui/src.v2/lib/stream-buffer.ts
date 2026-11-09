export type StreamMetadata = {
  visibility?: string;
  role?: string;
  phase?: string;
  source?: string;
  [key: string]: unknown;
} | undefined;

type FlushFn = (
  buffered: string,
  conversationId?: string,
  source?: string,
  metadata?: StreamMetadata,
  messageId?: string,
) => void;

export interface StreamBuffer {
  push: (chunk: string, conversationId?: string, source?: string, metadata?: StreamMetadata, messageId?: string) => void;
  flush: () => void;
  destroy: () => void;
}

export function createStreamBuffer(onFlush: FlushFn): StreamBuffer {
  let textBuf = "";
  let cidBuf: string | undefined;
  let sourceBuf: string | undefined;
  let messageIdBuf: string | undefined;
  let metadataBuf: StreamMetadata;
  let metadataKeyBuf = "";
  let activeStreamKey = "";
  let projectedFirstChunk = false;
  let rafId: number | null = null;
  let timeoutId: ReturnType<typeof setTimeout> | null = null;
  let generation = 0;

  const metadataKey = (metadata: StreamMetadata): string =>
    metadata ? JSON.stringify(metadata) : "";

  const streamKey = (
    conversationId: string | undefined,
    source: string | undefined,
    metadataKeyValue: string,
    messageId: string | undefined,
  ): string => JSON.stringify([conversationId, source, metadataKeyValue, messageId]);

  const clearBuffer = () => {
    textBuf = "";
    cidBuf = undefined;
    sourceBuf = undefined;
    messageIdBuf = undefined;
    metadataBuf = undefined;
    metadataKeyBuf = "";
  };

  const flushNow = () => {
    if (!textBuf) return;
    const buffered = textBuf;
    const conversationId = cidBuf;
    const source = sourceBuf;
    const metadata = metadataBuf;
    const messageId = messageIdBuf;
    // Detach the batch before invoking user code. A re-entrant push belongs to
    // the next batch, and a throwing consumer must not make this batch replay.
    clearBuffer();
    onFlush(buffered, conversationId, source, metadata, messageId);
  };

  const flush = () => {
    generation += 1;
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
    flushNow();
  };

  const scheduleFlush = () => {
    if (rafId === null) {
      const scheduledGeneration = generation;
      rafId = requestAnimationFrame(() => {
        if (scheduledGeneration !== generation) return;
        rafId = null;
        if (timeoutId !== null) {
          clearTimeout(timeoutId);
          timeoutId = null;
        }
        flushNow();
      });
      timeoutId = setTimeout(() => {
        if (scheduledGeneration !== generation) return;
        if (rafId !== null) {
          cancelAnimationFrame(rafId);
          rafId = null;
        }
        timeoutId = null;
        flushNow();
      }, 50);
    }
  };

  return {
    push(chunk, conversationId, source, metadata, messageId) {
      if (!chunk) return;
      // A source change within a pending batch is treated like a conversation
      // change: flush first so the previous origin keeps its attribution.
      const nextMetadataKey = metadataKey(metadata);
      const nextStreamKey = streamKey(conversationId, source, nextMetadataKey, messageId);
      if (
        (cidBuf !== conversationId ||
          sourceBuf !== source ||
          messageIdBuf !== messageId ||
          metadataKeyBuf !== nextMetadataKey) &&
        textBuf
      ) {
        flush();
      }
      if (activeStreamKey !== nextStreamKey) {
        activeStreamKey = nextStreamKey;
        projectedFirstChunk = false;
      }
      cidBuf = conversationId;
      sourceBuf = source;
      messageIdBuf = messageId;
      metadataBuf = metadata;
      metadataKeyBuf = nextMetadataKey;
      // The first visible provider chunk should reach React in the same task
      // that received the WebSocket event. Subsequent chunks still coalesce to
      // one store update per animation frame.
      if (!projectedFirstChunk) {
        projectedFirstChunk = true;
        onFlush(chunk, conversationId, source, metadata, messageId);
        return;
      }
      textBuf += chunk;
      scheduleFlush();
    },
    flush,
    destroy() {
      flush();
      activeStreamKey = "";
      projectedFirstChunk = false;
    },
  };
}
