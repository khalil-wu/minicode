export type StreamMetadata = {
  visibility?: string;
  role?: string;
  phase?: string;
} | undefined;

type FlushFn = (
  buffered: string,
  conversationId?: string,
  source?: string,
  metadata?: StreamMetadata,
) => void;

export interface StreamBuffer {
  push: (chunk: string, conversationId?: string, source?: string, metadata?: StreamMetadata) => void;
  flush: () => void;
  destroy: () => void;
}

export function createStreamBuffer(onFlush: FlushFn): StreamBuffer {
  let textBuf = "";
  let cidBuf: string | undefined;
  let sourceBuf: string | undefined;
  let metadataBuf: StreamMetadata;
  let metadataKeyBuf = "";
  let rafId: number | null = null;
  let generation = 0;

  const metadataKey = (metadata: StreamMetadata): string =>
    metadata ? JSON.stringify(metadata) : "";

  const flush = () => {
    generation += 1;
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    if (textBuf) {
      onFlush(textBuf, cidBuf, sourceBuf, metadataBuf);
      textBuf = "";
      sourceBuf = undefined;
      metadataBuf = undefined;
      metadataKeyBuf = "";
    }
  };

  const scheduleFlush = () => {
    if (rafId === null) {
      const scheduledGeneration = generation;
      rafId = requestAnimationFrame(() => {
        if (scheduledGeneration !== generation) return;
        rafId = null;
        if (textBuf) {
          onFlush(textBuf, cidBuf, sourceBuf, metadataBuf);
          textBuf = "";
          sourceBuf = undefined;
          metadataBuf = undefined;
          metadataKeyBuf = "";
        }
      });
    }
  };

  return {
    push(chunk, conversationId, source, metadata) {
      // A source change within a pending batch is treated like a conversation
      // change: flush first so the previous origin keeps its attribution.
      const nextMetadataKey = metadataKey(metadata);
      if ((cidBuf !== conversationId || sourceBuf !== source || metadataKeyBuf !== nextMetadataKey) && textBuf) {
        flush();
      }
      cidBuf = conversationId;
      sourceBuf = source;
      metadataBuf = metadata;
      metadataKeyBuf = nextMetadataKey;
      textBuf += chunk;
      scheduleFlush();
    },
    flush,
    destroy() {
      generation += 1;
      if (rafId !== null) cancelAnimationFrame(rafId);
      rafId = null;
      textBuf = "";
      cidBuf = undefined;
      sourceBuf = undefined;
      metadataBuf = undefined;
      metadataKeyBuf = "";
    },
  };
}
