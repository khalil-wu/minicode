type FlushFn = (buffered: string, conversationId?: string) => void;

export interface StreamBuffer {
  push: (chunk: string, conversationId?: string) => void;
  flush: () => void;
  destroy: () => void;
}

export function createStreamBuffer(onFlush: FlushFn): StreamBuffer {
  let textBuf = "";
  let cidBuf: string | undefined;
  let rafId: number | null = null;
  let generation = 0;

  const flush = () => {
    generation += 1;
    if (rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
    }
    if (textBuf) {
      onFlush(textBuf, cidBuf);
      textBuf = "";
    }
  };

  const scheduleFlush = () => {
    if (rafId === null) {
      const scheduledGeneration = generation;
      rafId = requestAnimationFrame(() => {
        if (scheduledGeneration !== generation) return;
        rafId = null;
        if (textBuf) {
          onFlush(textBuf, cidBuf);
          textBuf = "";
        }
      });
    }
  };

  return {
    push(chunk, conversationId) {
      if (cidBuf !== conversationId && textBuf) {
        flush();
      }
      cidBuf = conversationId;
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
    },
  };
}
