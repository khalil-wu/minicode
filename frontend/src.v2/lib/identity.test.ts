import { describe, expect, it } from "vitest";
import { eventMessageId, stableTextHash } from "./identity";

describe("identity helpers", () => {
  it("normalizes snake-case and camel-case message ownership", () => {
    expect(eventMessageId({ message_id: "  message-1  " })).toBe("message-1");
    expect(eventMessageId({ messageId: "message-2" })).toBe("message-2");
    expect(eventMessageId({ message_id: "" })).toBeUndefined();
  });

  it("keeps projection hashes stable", () => {
    expect(stableTextHash("same projection input")).toBe(stableTextHash("same projection input"));
    expect(stableTextHash("same projection input")).not.toBe(stableTextHash("different projection input"));
  });
});
