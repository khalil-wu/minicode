import { describe, expect, it } from "vitest";
import * as fc from "fast-check";
import {
  aggregateDiffBadge,
  normalizeToolDiff,
  reduceToolCallResult,
  reduceToolCallStart,
  type ToolCallRecord,
} from "./tool-call-reducer";

describe("toolCallReducer", () => {
  it("start adds an entry and never decreases the map size", () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            id: fc.string({ minLength: 1, maxLength: 12 }),
            name: fc.constantFrom("read_file", "write_file", "grep", "run_command"),
          }),
          { minLength: 1, maxLength: 20 },
        ),
        (calls) => {
          let map = new Map<string, ToolCallRecord>();
          for (const c of calls) {
            const before = map.size;
            map = reduceToolCallStart(map, {
              type: "tool_call",
              id: c.id,
              name: c.name,
              args: {},
            });
            if (map.size < before) return false;
          }
          return true;
        },
      ),
    );
  });

  it("start always sets status to 'running'", () => {
    fc.assert(
      fc.property(fc.string({ minLength: 1, maxLength: 12 }), (id) => {
        const map = reduceToolCallStart(new Map(), {
          type: "tool_call",
          id,
          name: "read_file",
          args: {},
        });
        return map.get(id)?.status === "running";
      }),
    );
  });

  it("result on a known id transitions to 'success' and preserves args", () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 12 }),
        fc.string({ maxLength: 200 }),
        (id, summary) => {
          const m1 = reduceToolCallStart(new Map(), {
            type: "tool_call",
            id,
            name: "read_file",
            args: { path: "x" },
          });
          const m2 = reduceToolCallResult(m1, {
            type: "tool_result",
            id,
            summary,
          });
          const r = m2.get(id);
          return (
            r?.status === "success" &&
            (r?.args as { path: string }).path === "x" &&
            r?.summary === summary
          );
        },
      ),
    );
  });

  it("does not revive or erase a terminal tool when a late start event arrives", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "late-start",
      name: "run_command",
      args: { command: "npm test" },
      started_at: 10,
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "late-start",
      summary: "Tests failed",
      status: "failed",
      content_preview: "1 failed",
    }, 20);

    const afterLateStart = reduceToolCallStart(finished, {
      type: "tool_call",
      id: "late-start",
      name: "run_command",
      args: { command: "npm test" },
    }, 30);

    expect(afterLateStart.get("late-start")).toMatchObject({
      status: "failed",
      summary: "Tests failed",
      contentPreview: "1 failed",
      startedAt: 10,
      finishedAt: 20,
    });
  });

  it("preserves web evidence metadata from tool_result events", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "fetch",
      name: "web_fetch",
      args: { url: "https://example.com/weather" },
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "fetch",
      summary: "已抓取页面",
      source_url: "https://example.com/weather",
      extraction_status: "partial",
      content_preview: "北京 18.3℃ 西南风",
      evidence_type: "fetched",
    });

    expect(finished.get("fetch")).toMatchObject({
      status: "success",
      sourceUrl: "https://example.com/weather",
      extractionStatus: "partial",
      contentPreview: "北京 18.3℃ 西南风",
      evidenceType: "fetched",
    });
  });

  it("projects typed artifact metadata without carrying image bytes", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "browser-shot",
      name: "browser_control",
      args: { action: "screenshot" },
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "browser-shot",
      summary: "Screenshot captured.",
      artifact_id: "art_screen",
      artifact_kind: "image",
      artifact_media_type: "image/png",
      artifact_bytes: 1234,
    });

    expect(finished.get("browser-shot")).toMatchObject({
      artifactId: "art_screen",
      artifactKind: "image",
      artifactMediaType: "image/png",
      artifactBytes: 1234,
    });
    expect(finished.get("browser-shot")).not.toHaveProperty("data");
  });

  it("preserves partial status from tool_result events", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "cmd-timeout",
      name: "run_command",
      args: { command: "npm test" },
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "cmd-timeout",
      summary: "Tool timed out after partial output.",
      status: "partial",
      limitation: "timeout",
    });

    expect(finished.get("cmd-timeout")).toMatchObject({
      status: "partial",
      limitation: "timeout",
    });
  });

  it("preserves timeout status from tool_result events", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "read-timeout",
      name: "read_file",
      args: { file_path: "huge.log" },
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "read-timeout",
      summary: "Tool timed out before producing a complete result.",
      status: "timeout",
      is_error: true,
      limitation: "timeout",
    });

    expect(finished.get("read-timeout")).toMatchObject({
      status: "timeout",
      limitation: "timeout",
    });
  });

  it("preserves turn metadata from start and result events", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "read",
      name: "read_file",
      args: { path: "first.ts" },
      turn_id: "assistant-turn-1",
      iteration_id: "iter:1",
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "read",
      summary: "Read first.ts",
      turn_id: "assistant-turn-1",
      iteration_id: "iter:1",
    });

    expect(finished.get("read")).toMatchObject({
      turnId: "assistant-turn-1",
      iterationId: "iter:1",
      summary: "Read first.ts",
    });
  });

  it("lets a failed result promote a debug tool into the timeline", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "tool-search",
      name: "tool_search",
      args: { query: "select:read_file" },
      visibility: "debug",
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "tool-search",
      summary: "Tool registry is unavailable",
      status: "failed",
      is_error: true,
      visibility: "timeline",
    });

    expect(finished.get("tool-search")).toMatchObject({
      status: "failed",
      visibility: "timeline",
    });
  });

  it("preserves structured error info from tool_result events", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "fetch-error",
      name: "web_fetch",
      args: {},
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "fetch-error",
      summary: "Tool 'web_fetch' is missing required argument(s): url",
      status: "blocked",
      is_error: true,
      error_info: {
        code: "tool.schema.missing_required",
        category: "validation",
        user_message: "工具调用缺少必要参数。",
        model_observation: "Repair the args before retrying.",
        developer_detail: "Tool 'web_fetch' is missing required argument(s): url",
        recoverable: true,
      },
    });

    expect(finished.get("fetch-error")?.errorInfo).toMatchObject({
      code: "tool.schema.missing_required",
      category: "validation",
      user_message: "工具调用缺少必要参数。",
    });
  });

  it("result on an unknown id is a no-op (map stays equal)", () => {
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 6 }),
        fc.string({ minLength: 1, maxLength: 6 }),
        (knownId, unknownId) => {
          fc.pre(knownId !== unknownId);
          const m1 = reduceToolCallStart(new Map(), {
            type: "tool_call",
            id: knownId,
            name: "x",
            args: {},
          });
          const m2 = reduceToolCallResult(m1, {
            type: "tool_result",
            id: unknownId,
            summary: "irrelevant",
          });
          return m1.size === m2.size && m2.get(knownId)?.status === "running";
        },
      ),
    );
  });

  it("preserves backend-projected activity kind on tool start", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "custom-read",
      name: "custom_workspace_reader",
      args: {},
      result_kind: "file",
      activity_kind: "fileRead",
    });

    expect(started.get("custom-read")).toMatchObject({
      resultKind: "file",
      activityKind: "fileRead",
    });
  });

  it("blocked result updates an existing tool call without losing lifecycle metadata", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "blocked",
      name: "run_command",
      args: { command: "echo hi > out.txt" },
      started_at: 123,
      display_hint: "Running command",
      input_summary: "echo hi > out.txt",
      iteration_id: "iter:1",
      phase: "tool",
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "blocked",
      summary: "Blocked run_command because it appears to create or edit files through the shell.",
      status: "blocked",
      is_error: true,
      display_summary: "Blocked tool: run_command",
      result_kind: "command",
      duration_ms: 7,
      iteration_id: "iter:1",
      phase: "tool",
    }, 456);

    expect(finished.get("blocked")).toMatchObject({
      id: "blocked",
      name: "run_command",
      status: "blocked",
      startedAt: 123,
      finishedAt: 456,
      displayHint: "Running command",
      inputSummary: "echo hi > out.txt",
      displaySummary: "Blocked tool: run_command",
      resultKind: "command",
      durationMs: 7,
      iterationId: "iter:1",
      phase: "tool",
    });
  });

  it("aggregateDiffBadge sums explicit diffs", () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            plus: fc.integer({ min: 0, max: 100 }),
            minus: fc.integer({ min: 0, max: 100 }),
          }),
          { minLength: 1, maxLength: 10 },
        ),
        (entries) => {
          const records: ToolCallRecord[] = entries.map((e, i) => ({
            id: `r${i}`,
            name: "edit_file",
            args: {},
            status: "success",
            startedAt: 0,
            diff: e,
          }));
          const sumPlus = entries.reduce((s, e) => s + e.plus, 0);
          const sumMinus = entries.reduce((s, e) => s + e.minus, 0);
          const result = aggregateDiffBadge(records);
          return result.plus === sumPlus && result.minus === sumMinus;
        },
      ),
    );
  });

  it("normalizes structured tool_result diffs for edit badges", () => {
    const started = reduceToolCallStart(new Map(), {
      type: "tool_call",
      id: "edit",
      name: "edit_file",
      args: { file_path: "backend/server.py" },
    });
    const finished = reduceToolCallResult(started, {
      type: "tool_result",
      id: "edit",
      summary: "Edited backend/server.py",
      diff: {
        format: "structured",
        stats: { additions: 7, deletions: 3 },
        files: [
          {
            path: "backend/server.py",
            additions: 7,
            deletions: 3,
            patch: "--- a/backend/server.py\n+++ b/backend/server.py\n@@\n-old\n+new",
          },
        ],
      },
    });

    expect(finished.get("edit")?.diff).toEqual({
      plus: 7,
      minus: 3,
      patch: "--- a/backend/server.py\n+++ b/backend/server.py\n@@\n-old\n+new",
      files: [{
        path: "backend/server.py",
        oldPath: undefined,
        plus: 7,
        minus: 3,
        patch: "--- a/backend/server.py\n+++ b/backend/server.py\n@@\n-old\n+new",
        status: undefined,
      }],
    });
  });

  it("infers diff stats from patch text when structured counts are missing", () => {
    const patch = [
      "diff --git a/train_transformer.py b/train_transformer.py",
      "--- a/train_transformer.py",
      "+++ b/train_transformer.py",
      "@@ -1,2 +1,3 @@",
      " import torch",
      "-old_model = MLP()",
      "+model = TransformerClassifier()",
      "+optimizer = torch.optim.AdamW(model.parameters())",
    ].join("\n");

    expect(normalizeToolDiff({ patch })).toEqual({
      plus: 2,
      minus: 1,
      patch,
    });
  });

  it("aggregates per-file plus/minus fields even when no combined patch exists", () => {
    expect(normalizeToolDiff({
      files: [
        { path: "src/a.ts", plus: 4, minus: 1 },
        { path: "src/b.ts", plus: 2, minus: 3 },
      ],
    })).toEqual({
      plus: 6,
      minus: 4,
      patch: undefined,
      files: [
        { path: "src/a.ts", oldPath: undefined, plus: 4, minus: 1, patch: undefined, status: undefined },
        { path: "src/b.ts", oldPath: undefined, plus: 2, minus: 3, patch: undefined, status: undefined },
      ],
    });
  });

  it("preserves the original path for renamed files", () => {
    expect(normalizeToolDiff({
      stats: { additions: 0, deletions: 0 },
      files: [{
        path: "src/new-name.ts",
        old_path: "src/old-name.ts",
        status: "renamed",
      }],
    })?.files).toEqual([{
      path: "src/new-name.ts",
      oldPath: "src/old-name.ts",
      plus: 0,
      minus: 0,
      patch: undefined,
      status: "renamed",
    }]);
  });

  it("keeps structured files when aggregate stats or a combined patch are also present", () => {
    expect(normalizeToolDiff({
      plus: 3,
      minus: 1,
      patch: "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-old\n+new",
      files: [{ path: "src/a.ts", additions: 3, deletions: 1, status: "modified" }],
    })).toEqual({
      plus: 3,
      minus: 1,
      patch: "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-old\n+new",
      files: [{
        path: "src/a.ts",
        oldPath: undefined,
        plus: 3,
        minus: 1,
        patch: undefined,
        status: "modified",
      }],
    });
  });

  it("aggregateDiffBadge returns 0/0 for empty input", () => {
    expect(aggregateDiffBadge([])).toEqual({ plus: 0, minus: 0 });
  });
});
