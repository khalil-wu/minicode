import { describe, expect, it } from "vitest";
import { hydrateMessages, type BackendTranscriptMessage } from "./transcriptHydration";

describe("hydrateMessages", () => {
  it("preserves scheduled-task origin metadata on hydrated user messages", () => {
    const messages = hydrateMessages([{
      id: "scheduled-user-message",
      role: "user",
      content: "run the nightly check",
      metadata: {
        source: "scheduled_task",
        scheduled_task_id: "task-nightly",
        scheduled_run_id: "run-2026-09-03",
      },
      timestamp: "2026-09-03T00:00:00Z",
    }]);

    expect(messages[0]).toMatchObject({
      messageSource: {
        kind: "scheduled_task",
        taskId: "task-nightly",
        runId: "run-2026-09-03",
      },
    });
  });

  it("restores steer provenance for persisted user turns", () => {
    const [message] = hydrateMessages([{
      id: "user-steer",
      role: "user",
      content: "change direction",
      steered: true,
      steer_target_message_id: "assistant-current",
    }]);

    expect(message).toMatchObject({
      id: "user-steer",
      steeredIntoMessageId: "assistant-current",
    });
  });

  it("restores structured skill and plugin mentions on user turns", () => {
    const [message] = hydrateMessages([{
      id: "user-context",
      role: "user",
      content: "Use these",
      context_refs: [
        { kind: "skill", name: "review", path: "C:/skills/review/SKILL.md" },
        { kind: "plugin", name: "docs", config_name: "docs", path: "plugin://docs" },
        { kind: "plugin", name: "forged", config_name: "forged", path: "https://invalid" },
      ],
    }]);

    expect(message.contextRefs).toEqual([
      { kind: "skill", name: "review", path: "C:/skills/review/SKILL.md" },
      { kind: "plugin", name: "docs", configName: "docs", path: "plugin://docs" },
    ]);
  });

  it("restores terminal status and failure metadata", () => {
    const [message] = hydrateMessages([{
      id: "assistant-partial",
      role: "assistant",
      content: "部分结果",
      terminal_status: "partial",
      termination_reason: "max_iterations",
      failure_message: "达到迭代上限",
      failure_recoverable: true,
      completed_at: 123,
    }]);

    expect(message.terminalStatus).toBe("partial");
    expect(message.failureMessage).toBe("达到迭代上限");
    expect(message.failureRecoverable).toBe(true);
    expect(message.completedAt).toBe(123);
  });

  it("preserves generated-image text anchors without restoring a base64 body", () => {
    const [message] = hydrateMessages([{
      id: "assistant-image-history",
      role: "assistant",
      content: "开场文字\n\n完成文字",
      artifacts: [{
        artifactId: "artifact-image-history",
        kind: "image",
        summary: "Generated PNG image",
        mediaType: "image/png",
        textOffset: 6,
      }],
    } as BackendTranscriptMessage]);

    expect(message.artifacts).toEqual([{
      artifactId: "artifact-image-history",
      kind: "image",
      summary: "Generated PNG image",
      mediaType: "image/png",
      textOffset: 6,
    }]);
    expect(message.artifacts[0]?.url).toBeUndefined();
  });
  it("keeps consecutive user messages with identical text when their ids differ", () => {
    const messages = hydrateMessages([
      { id: "user-1", role: "user", content: "继续" },
      { id: "user-2", role: "user", content: "继续" },
    ]);

    expect(messages.map((message) => message.id)).toEqual(["user-1", "user-2"]);
  });

  it("preserves partial tool results from blocks-first transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-partial-block",
      role: "assistant",
      content: "I used the partial output.",
      blocks: [
        {
          type: "tool_call",
          record: {
            id: "read-timeout",
            name: "read_file",
            args: { file_path: "large.log" },
            status: "partial",
            summary: "Timed out; partial output preserved.",
          },
        },
        { type: "text", content: "I used the partial output." },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: { id: "read-timeout", status: "partial" },
    });
  });

  it("keeps legacy screenshot MIME aliases on tool records", () => {
    const [message] = hydrateMessages([{
      id: "assistant-legacy-screenshot",
      role: "assistant",
      content: "截图已完成",
      blocks: [{
        type: "tool_call",
        record: {
          id: "browser-shot-legacy",
          name: "browser_control",
          args: { action: "screenshot" },
          status: "success",
          artifact_id: "artifact-legacy-screenshot",
          mime_type: "image/png; charset=binary",
        },
      }],
    }]);

    expect(message.blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: {
        artifactId: "artifact-legacy-screenshot",
        artifactMediaType: "image/png; charset=binary",
      },
    });
  });

  it("preserves timeout tool results from blocks-first transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-timeout-block",
      role: "assistant",
      content: "",
      blocks: [
        {
          type: "tool_call",
          record: {
            id: "read-timeout",
            name: "read_file",
            args: { file_path: "large.log" },
            status: "timeout",
            summary: "Timed out before a complete result was available.",
            limitation: "timeout",
          },
        },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: { id: "read-timeout", status: "timeout", limitation: "timeout" },
    });
  });

  it("preserves tool projection metadata from persisted transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-tool-metadata",
      role: "assistant",
      content: "Done.",
      blocks: [
        {
          type: "tool_call",
          record: {
            id: "write-readme",
            name: "write_file",
            args: { file_path: "README.md" },
            status: "success",
            summary: "Wrote README.md",
            resultKind: "edit",
            activityKind: "fileChange",
            displayScope: "activity",
            outputPreview: "stdout",
            stdoutPreview: "stdout",
            stderrPreview: "stderr",
            providerErrorType: "network",
            errorKind: "permission_required",
            userSummary: "User-facing",
            developerDetail: "Developer detail",
            projection: "warning",
            recoverable: false,
            errorInfo: { code: "permission_required" },
            diff: {
              patch: "diff --git a/README.md b/README.md\n@@ -0,0 +1 @@\n+# Project",
              plus: 0,
              minus: 0,
            },
          },
        },
        { type: "text", content: "Done.", source: "model_final", visibility: "final" },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: {
        id: "write-readme",
        resultKind: "edit",
        activityKind: "fileChange",
        outputPreview: "stdout",
        stdoutPreview: "stdout",
        stderrPreview: "stderr",
        providerErrorType: "network",
        errorKind: "permission_required",
        userSummary: "User-facing",
        developerDetail: "Developer detail",
        projection: "warning",
        recoverable: false,
        errorInfo: { code: "permission_required" },
        diff: expect.objectContaining({ plus: 1, minus: 0 }),
      },
    });
  });

  it("preserves skill process metadata from persisted transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-skill-process",
      role: "assistant",
      content: "",
      blocks: [
        {
          type: "process",
          id: "skill:frontend-dev:loaded",
          itemKind: "skill",
          content: "已加载 Skill: frontend-dev。",
          title: "已加载 Skill: frontend-dev",
          summary: "匹配触发词: react",
          source: "runtime",
          status: "completed",
          visibility: "timeline",
          displayScope: "activity",
          skillName: "frontend-dev",
          triggerMode: "implicit",
          sourceLevel: "project",
          reason: "匹配触发词: react",
          tokenEstimate: 7,
          timestamp: 123,
        },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "process",
      id: "skill:frontend-dev:loaded",
      itemKind: "skill",
      skillName: "frontend-dev",
      triggerMode: "implicit",
      sourceLevel: "project",
      reason: "匹配触发词: react",
      tokenEstimate: 7,
    });
  });

  it.each(["subagent", "cache"] as const)("preserves the %s progress phase from persisted transcripts", (phase) => {
    const messages = hydrateMessages([{
      id: `assistant-${phase}`,
      role: "assistant",
      content: "",
      blocks: [{
        type: "progress",
        id: `progress-${phase}`,
        stage: "status",
        phase,
        status: "completed",
        message: `${phase} complete`,
        visibility: "timeline",
      }],
      timestamp: 1,
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({ type: "progress", phase });
  });

  it("preserves partial tool results from legacy tool_calls transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-partial-legacy",
      role: "assistant",
      content: "I used the partial output.",
      tool_calls: [{
        id: "fetch-timeout",
        name: "web_fetch",
        args: { url: "https://example.test/large" },
        status: "partial",
        summary: "Fetch timed out after partial extraction.",
      }],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: { id: "fetch-timeout", status: "partial" },
    });
  });

  it("preserves citation url and title metadata from transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-cited",
      role: "assistant",
      content: "Answer with citation. [1]",
      citations: [{
        source: "https://example.test/weather",
        url: "https://example.test/weather",
        title: "Weather source",
        label: "example.test",
        range: [0, 0],
      }],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages).toHaveLength(1);
    expect(messages[0].citations).toEqual([{
      source: "https://example.test/weather",
      url: "https://example.test/weather",
      title: "Weather source",
      label: "example.test",
      range: [0, 0],
    }]);
  });

  it("preserves text routing metadata from blocks-first transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-routed-text",
      role: "assistant",
      content: "最终答案。",
      blocks: [
        {
          type: "text",
          content: "我先检查这里。",
          source: "model_preamble",
          visibility: "timeline",
          role: "assistant",
          phase: "model",
        },
        {
          type: "text",
          content: "最终答案。",
          source: "model_final",
          visibility: "final",
          role: "assistant",
          phase: "final",
        },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks).toEqual([
      expect.objectContaining({
        id: "legacy-process-0",
        itemKind: "process_text",
        type: "process",
        content: "我先检查这里。",
        source: "model_preamble",
        visibility: "timeline",
        role: "assistant",
      }),
      expect.objectContaining({
        type: "text",
        content: "最终答案。",
        source: "model_final",
        status: "completed",
      }),
    ]);
  });

  it("filters raw provider reasoning from blocks-first transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-provider-thinking",
      role: "assistant",
      content: "最终答案。",
      blocks: [
        {
          type: "thinking",
          content: "厂家 reasoning",
          source: "provider",
          visibility: "timeline",
          is_raw_provider_reasoning: true,
          provider_reasoning_type: "reasoning_text",
        },
        {
          type: "text",
          content: "最终答案。",
          source: "model_final",
          visibility: "final",
          phase: "final",
        },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks).toEqual([
      expect.objectContaining({
        type: "text",
        content: "最终答案。",
        source: "model_final",
      }),
    ]);
  });

  it("restores provider reasoning summaries but drops untyped legacy provider thinking", () => {
    const messages = hydrateMessages([{
      id: "assistant-provider-summary",
      role: "assistant",
      content: "最终答案。",
      blocks: [
        {
          type: "thinking",
          content: "legacy raw body",
          source: "provider",
        },
        {
          type: "thinking",
          content: "持久摘要",
          source: "provider",
          provider_reasoning_type: "reasoning_summary_text",
          visibility: "timeline",
        },
        {
          type: "text",
          content: "最终答案。",
          source: "model_final",
          visibility: "final",
          phase: "final",
        },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks).toEqual([
      expect.objectContaining({
        type: "thinking",
        content: "持久摘要",
        providerReasoningType: "reasoning_summary_text",
      }),
      expect.objectContaining({
        type: "text",
        content: "最终答案。",
        source: "model_final",
      }),
    ]);
  });

  it("preserves assistant completion timestamps from transcripts", () => {
    const messages = hydrateMessages([{
      id: "assistant-completed-at",
      role: "assistant",
      content: "Done.",
      timestamp: "2026-06-21T00:00:00.000Z",
      completed_at: "2026-06-21T00:00:28.000Z",
    }]);

    expect(messages[0]?.timestamp).toBe(Date.parse("2026-06-21T00:00:00.000Z"));
    expect(messages[0]?.completedAt).toBe(Date.parse("2026-06-21T00:00:28.000Z"));
  });

  it("keeps interrupted persisted work partial after reload", () => {
    const [message] = hydrateMessages([{
      id: "assistant-interrupted",
      role: "assistant",
      terminal_status: "cancelled",
      blocks: [
        {
          type: "progress",
          id: "progress-1",
          stage: "tool",
          status: "partial",
          message: "Running tool",
        },
        {
          type: "process",
          id: "process-1",
          itemKind: "process_text",
          status: "partial",
          content: "Working",
        },
        {
          type: "tool_call",
          record: {
            id: "tool-1",
            name: "read_file",
            args: { file_path: "README.md" },
            status: "partial",
          },
        },
      ],
    }]);

    expect(message.terminalStatus).toBe("interrupted");
    expect(message.blocks).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "progress", status: "partial" }),
      expect.objectContaining({ type: "process", status: "partial" }),
      expect.objectContaining({
        type: "tool_call",
        record: expect.objectContaining({ status: "partial" }),
      }),
    ]));
  });

  it("merges legacy duplicate pending and terminal blocks by tool-use id", () => {
    const [message] = hydrateMessages([{
      id: "assistant-duplicate-tool",
      role: "assistant",
      content: "",
      blocks: [
        {
          type: "tool_call",
          record: {
            id: "write-1",
            name: "write_file",
            args: {},
            status: "pending",
            turnId: "run-1",
            startedAt: 10,
          },
        },
        {
          type: "tool_call",
          record: {
            id: "write-1",
            name: "write_file",
            args: { file_path: "src/app.ts" },
            status: "success",
            turnId: "assistant-1",
            startedAt: 20,
            diff: { plus: 4, minus: 2 },
          },
        },
      ],
    }]);

    expect(message.blocks).toHaveLength(1);
    expect(message.blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: {
        id: "write-1",
        status: "success",
        startedAt: 10,
        args: { file_path: "src/app.ts" },
      },
    });
  });

  it("restores presented files and deleted temporary tool metadata", () => {
    const messages = hydrateMessages([{
      id: "assistant-deliverable",
      role: "assistant",
      content: "文档已创建。",
      reply_attachments: [{ path: "C:\\Desktop\\report.pdf", size: 4096, is_image: false }],
      tool_calls: [
        {
          id: "write-helper",
          name: "write_file",
          args: { file_path: "create_report.py" },
          status: "success",
          temporaryRemoved: true,
          displayScope: "silent",
        },
        {
          id: "present-report",
          name: "present_file",
          args: { path: "C:\\Desktop\\report.pdf" },
          status: "success",
          outputFiles: [{
            path: "C:\\Desktop\\report.pdf",
            name: "report.pdf",
            size: 4096,
            mimeType: "application/pdf",
            isImage: false,
          }],
        },
      ],
    }]);

    expect(messages[0].replyAttachments).toEqual([{
      path: "C:\\Desktop\\report.pdf",
      size: 4096,
      isImage: false,
    }]);
    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "tool_call",
      record: { temporaryRemoved: true },
    });
    expect(messages[0].blocks?.[1]).toMatchObject({
      type: "tool_call",
      record: {
        outputFiles: [{
          path: "C:\\Desktop\\report.pdf",
          name: "report.pdf",
          size: 4096,
          mimeType: "application/pdf",
          isImage: false,
        }],
      },
    });
  });

  it("hydrates process_text blocks as visible timeline process blocks", () => {
    const messages = hydrateMessages([{
      id: "assistant-process-text",
      role: "assistant",
      content: "完成。",
      blocks: [
        {
          type: "process",
          id: "preamble-1",
          item_kind: "process_text",
          content: "我会先看路由层，再查恢复路径。",
          source: "model_preamble",
          role: "assistant",
          visibility: "timeline",
          status: "completed",
          loop_id: "loop-1",
          iteration_id: "iter-1",
          tool_call_ids: ["tool-1"],
          timestamp: "2026-05-27T00:00:00Z",
        },
        {
          type: "text",
          content: "完成。",
          source: "model_final",
          visibility: "final",
          phase: "final",
        },
      ],
      timestamp: "2026-05-27T00:00:00Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "process",
      id: "preamble-1",
      itemKind: "process_text",
      content: "我会先看路由层，再查恢复路径。",
      source: "model_preamble",
      role: "assistant",
      visibility: "timeline",
      status: "completed",
      loopId: "loop-1",
      iterationId: "iter-1",
      toolCallIds: ["tool-1"],
    });
    expect(messages[0].blocks?.[1]).toMatchObject({
      type: "text",
      source: "model_final",
      status: "completed",
    });
  });

  it("canonicalizes a structured text block matching authoritative assistant content", () => {
    const messages = hydrateMessages([{
      id: "assistant-restored-weather",
      role: "assistant",
      content: "根据中央气象台预报，北京今天雷阵雨。",
      blocks: [
        {
          type: "process",
          id: "preamble-weather",
          item_kind: "process_text",
          content: "我来查一下今天北京的天气。",
          source: "model_preamble",
          visibility: "timeline",
          status: "completed",
        },
        {
          type: "tool_call",
          record: {
            id: "search-weather",
            name: "web_search",
            args: { query: "北京天气" },
            status: "success",
          },
        },
        {
          type: "text",
          content: "根据中央气象台预报，北京今天雷阵雨。",
        },
      ],
      timestamp: "2026-06-23T08:14:33.504Z",
    }]);

    expect(messages[0].blocks?.[2]).toMatchObject({
      type: "text",
      content: "根据中央气象台预报，北京今天雷阵雨。",
      source: "model_final",
      status: "completed",
    });
  });

  it("does not promote persisted unsealed stream text from message content", () => {
    const messages = hydrateMessages([{
      id: "assistant-restored-unsealed",
      role: "assistant",
      content: "根据中央气象台预报，北京今天雷阵雨。",
      blocks: [
        {
          type: "process",
          id: "preamble-weather",
          item_kind: "process_text",
          content: "我来查一下今天北京的天气。",
          source: "model_preamble",
          visibility: "timeline",
          status: "completed",
        },
        {
          type: "text",
          content: "根据中央气象台预报，北京今天雷阵雨。",
          source: "stream",
          visibility: "unsealed",
          phase: "model",
        },
      ],
      timestamp: "2026-06-23T08:14:33.504Z",
    }]);

    expect(messages[0].blocks?.[1]).toMatchObject({
      type: "text",
      content: "根据中央气象台预报，北京今天雷阵雨。",
      source: "stream",
    });
  });

  it("does not promote legacy draft text to a final answer", () => {
    const messages = hydrateMessages([{
      id: "assistant-restored-draft",
      role: "assistant",
      content: "这只是草稿。",
      blocks: [
        {
          type: "text",
          content: "这只是草稿。",
          source: "stream",
          visibility: "draft",
          phase: "model",
        },
      ],
      timestamp: "2026-06-23T08:14:33.504Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      type: "text",
      content: "这只是草稿。",
      source: "stream",
    });
  });

  it("does not repair a text block that only matches after whitespace collapsing", () => {
    const messages = hydrateMessages([{
      id: "assistant-restored-whitespace",
      role: "assistant",
      content: "Hello World",
      blocks: [{
        type: "text",
        content: "Hello\nWorld",
        source: "stream",
        visibility: "unsealed",
        phase: "model",
      }],
      timestamp: "2026-06-23T08:14:33.504Z",
    }]);

    expect(messages[0].blocks?.[0]).toMatchObject({
      source: "stream",
    });
  });

  it("keeps transcript system record labels separate from information-bearing content", () => {
    const messages = hydrateMessages([{
      id: "bash-record",
      role: "bashExecution",
      command: "npm test",
      output: "42 tests passed",
      exitCode: 0,
      timestamp: 10,
    } as BackendTranscriptMessage]);

    expect(messages[0]).toMatchObject({
      role: "system",
      systemNoticeTitle: "命令执行记录",
      content: "$ npm test\n42 tests passed",
    });
  });
});
