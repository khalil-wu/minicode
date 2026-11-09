import { describe, expect, it, vi } from "vitest";
import { createStreamBuffer } from "./stream-buffer";

describe("createStreamBuffer", () => {
  it("flushes pending text once before destroy", () => {
    let callback: FrameRequestCallback | null = null;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      callback = cb;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const onFlush = vi.fn();

    const buffer = createStreamBuffer(onFlush);
    buffer.push("h", "c1");
    buffer.push("ello", "c1");
    buffer.destroy();
    callback?.(performance.now());

    expect(onFlush).toHaveBeenCalledTimes(2);
    expect(onFlush).toHaveBeenNthCalledWith(1, "h", "c1", undefined, undefined, undefined);
    expect(onFlush).toHaveBeenNthCalledWith(2, "ello", "c1", undefined, undefined, undefined);
    vi.unstubAllGlobals();
  });

  it("projects the first chunk synchronously and flushes the buffered continuation once", () => {
    let callback: FrameRequestCallback | null = null;
    vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
      callback = cb;
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const onFlush = vi.fn();

    const buffer = createStreamBuffer(onFlush);
    buffer.push("h", "c1");
    expect(onFlush).toHaveBeenCalledWith("h", "c1", undefined, undefined, undefined);
    buffer.push("ello", "c1");
    buffer.flush();
    callback?.(performance.now());

    expect(onFlush).toHaveBeenCalledTimes(2);
    expect(onFlush).toHaveBeenNthCalledWith(2, "ello", "c1", undefined, undefined, undefined);
    vi.unstubAllGlobals();
  });

  it("carries the source through to the flush callback", () => {
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const onFlush = vi.fn();
    const buffer = createStreamBuffer(onFlush);
    buffer.push("hi", "c1", "reply");
    buffer.flush();

    expect(onFlush).toHaveBeenCalledWith("hi", "c1", "reply", undefined, undefined);
    vi.unstubAllGlobals();
  });

  it("flushes pending text before a source change to preserve attribution", () => {
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const onFlush = vi.fn();
    const buffer = createStreamBuffer(onFlush);
    buffer.push("a", "c1", "stream");
    buffer.push("b", "c1", "reply");
    buffer.flush();

    // First flush keeps the original source; the second opens a new block.
    expect(onFlush).toHaveBeenNthCalledWith(1, "a", "c1", "stream", undefined, undefined);
    expect(onFlush).toHaveBeenNthCalledWith(2, "b", "c1", "reply", undefined, undefined);
    vi.unstubAllGlobals();
  });

  it("flushes pending text before a conversation change to avoid cross-conversation leaks", () => {
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const onFlush = vi.fn();
    const buffer = createStreamBuffer(onFlush);
    // Conversation A streams a chunk; before it flushes, conversation B's
    // chunk arrives with the same default source/metadata. The buffered A text
    // must flush to A, not be swept into B.
    buffer.push("a-text", "conv-a", "stream");
    buffer.push("b-text", "conv-b", "stream");
    buffer.flush();

    expect(onFlush).toHaveBeenNthCalledWith(1, "a-text", "conv-a", "stream", undefined, undefined);
    expect(onFlush).toHaveBeenNthCalledWith(2, "b-text", "conv-b", "stream", undefined, undefined);
    vi.unstubAllGlobals();
  });

  it("flushes pending text before a message id change to avoid cross-turn leaks", () => {
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const onFlush = vi.fn();
    const buffer = createStreamBuffer(onFlush);

    buffer.push("old", "conv-a", "stream", undefined, "assistant-old");
    buffer.push("new", "conv-a", "stream", undefined, "assistant-new");
    buffer.flush();

    expect(onFlush).toHaveBeenNthCalledWith(1, "old", "conv-a", "stream", undefined, "assistant-old");
    expect(onFlush).toHaveBeenNthCalledWith(2, "new", "conv-a", "stream", undefined, "assistant-new");
    vi.unstubAllGlobals();
  });

  it("falls back to a short timeout when animation frames do not fire", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const onFlush = vi.fn();
    const buffer = createStreamBuffer(onFlush);

    buffer.push("s", "conv-a", "stream");
    buffer.push("treamed", "conv-a", "stream");
    vi.advanceTimersByTime(49);
    expect(onFlush).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);

    expect(onFlush).toHaveBeenNthCalledWith(2, "treamed", "conv-a", "stream", undefined, undefined);
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
});
