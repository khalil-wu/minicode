import { describe, expect, it } from "vitest";
import { normalizeContextLedger } from "./contextLedger";

describe("normalizeContextLedger", () => {
  it("preserves native attachment accounting and rejects unknown categories", () => {
    expect(normalizeContextLedger({
      schema_version: 1,
      estimated_tokens: 2_048,
      actual_tokens: 1_920,
      compaction_count: 2,
      native_attachment_tokens: 1_280,
      native_attachment_count: 2,
      entries: [
        {
          category: "files_attachments",
          label: "Files & attachments",
          estimated_tokens: 1_280,
          item_count: 2,
          source_count: 2,
          sources: ["image/png", "spec.pdf"],
        },
        {
          category: "future_category",
          label: "Future",
          estimated_tokens: 99,
          item_count: 1,
          source_count: 0,
          sources: [],
        },
      ],
    })).toEqual({
      schema_version: 1,
      estimated_tokens: 2_048,
      actual_tokens: 1_920,
      compaction_count: 2,
      native_attachment_tokens: 1_280,
      native_attachment_count: 2,
      entries: [{
        category: "files_attachments",
        label: "Files & attachments",
        estimated_tokens: 1_280,
        item_count: 2,
        source_count: 2,
        sources: ["image/png", "spec.pdf"],
      }],
    });
  });

  it("hydrates legacy ledgers with schema v1 attachment defaults", () => {
    expect(normalizeContextLedger({
      estimated_tokens: 640,
      actual_tokens: 600,
      compaction_count: 1,
      entries: [{
        category: "history",
        label: "History",
        estimated_tokens: 640,
        item_count: 5,
        source_count: 0,
        sources: [],
      }],
    })).toMatchObject({
      schema_version: 1,
      native_attachment_tokens: 0,
      native_attachment_count: 0,
    });
  });

  it("rejects malformed ledger payloads instead of leaking untyped runtime data", () => {
    expect(normalizeContextLedger({ entries: "invalid" })).toBeNull();
    expect(normalizeContextLedger(null)).toBeNull();
  });
});
