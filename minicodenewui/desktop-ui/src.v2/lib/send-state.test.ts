import { describe, expect, it } from "vitest";
import * as fc from "fast-check";
import { deriveSendState } from "./send-state";

describe("deriveSendState", () => {
  it("queues explicit input while disconnected and disables an empty composer", () => {
    expect(
      deriveSendState({ hasContent: true, isStreaming: false, isConnected: false }),
    ).toBe("offline-queue");
    expect(
      deriveSendState({ hasContent: false, isStreaming: true, isConnected: false }),
    ).toBe("disabled");
    expect(
      deriveSendState({ hasContent: true, isStreaming: false, isConnected: false, hasModel: false }),
    ).toBe("disabled");
  });

  it("queues typed input while streaming and keeps stop available for an empty composer", () => {
    expect(
      deriveSendState({ hasContent: true, isStreaming: true, isConnected: true }),
    ).toBe("queue");
    expect(
      deriveSendState({ hasContent: false, isStreaming: true, isConnected: true }),
    ).toBe("stop");
  });

  it("returns 'idle' when connected, idle, with content", () => {
    expect(
      deriveSendState({ hasContent: true, isStreaming: false, isConnected: true }),
    ).toBe("idle");
  });

  it("returns 'disabled' when connected with content but no model", () => {
    expect(
      deriveSendState({ hasContent: true, isStreaming: false, isConnected: true, hasModel: false }),
    ).toBe("disabled");
  });

  it("returns 'disabled' when connected, idle, with no content", () => {
    expect(
      deriveSendState({ hasContent: false, isStreaming: false, isConnected: true }),
    ).toBe("disabled");
  });

  it("never returns an unexpected state", () => {
    const valid = new Set(["idle", "sending", "queue", "offline-queue", "stop", "disabled"]);
    fc.assert(
      fc.property(
        fc.boolean(),
        fc.boolean(),
        fc.boolean(),
        (hasContent, isStreaming, isConnected) => {
          const state = deriveSendState({ hasContent, isStreaming, isConnected });
          return valid.has(state);
        },
      ),
    );
  });
});
