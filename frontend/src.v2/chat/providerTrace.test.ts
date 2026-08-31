import { describe, expect, it } from "vitest";

import {
  providerCacheHitRate,
  providerContainerSummary,
  providerCurlSkeleton,
  providerDuplicateInputSummary,
  providerInstructionsTransportSummary,
  providerLargestInputItemsSummary,
  providerLargestToolsSummary,
  providerLoopMetricsSummary,
  providerNativeUsageDetails,
  providerOutputPhaseCounts,
  providerPromptCacheDiagnosticSummary,
  providerPromptLargestSections,
  providerPromptSectionDeltaSummary,
  providerPromptSectionSummary,
  providerRequestDiffSummary,
  providerRequestModeSummary,
  providerRefusalSummary,
  providerResponseLifecycle,
  providerSafeRequestPackage,
  providerSearchSourcesSummary,
  providerTimelineRows,
  providerTraceDiagnostics,
  providerTraceExportPackage,
  providerTraceExportJsonl,
  providerTracePayloadFromDone,
  providerTracePayloadFromExport,
  providerUsageSummary,
  hasProviderContainerMetadata,
  hasProviderRefusalMetadata,
  sanitizeProviderTraceExportValue,
} from "./providerTrace";
import type { ProviderRawMetadata } from "../stores/types";

describe("providerTrace helpers", () => {
  it("normalizes provider prompt-cache hit rates without double-counting OpenAI cached tokens", () => {
    expect(providerCacheHitRate({ input: 80, output: 0, cacheRead: 20, cacheWrite: 5, reasoning: 0 })).toBe(25);
    expect(providerCacheHitRate({ input: 80, ordinaryInput: 80, output: 0, cacheRead: 20, cacheWrite: 5, reasoning: 0, provider: "anthropic" })).toBe(19);

    const summary = providerUsageSummary({
      provider: "openai_responses",
      usage: {
        input_tokens: 34_081,
        output_tokens: 100,
        cache_read_input_tokens: 30_464,
        prompt_cache_total_tokens: 34_081,
        prompt_cache_hit_rate: 89.4,
      },
    });

    expect(providerCacheHitRate(summary)).toBe(89.4);
  });

  it("preserves provider cache-inclusion semantics across summaries and exports", () => {
    const raw: ProviderRawMetadata = {
      provider: "anthropic",
      usage: {
        input_tokens: 100,
        output_tokens: 10,
        cache_read_input_tokens: 600,
        cache_creation_input_tokens: 300,
        ordinary_input_tokens: 100,
        prompt_cache_total_tokens: 1_000,
        input_includes_cache_read: false,
        input_includes_cache_write: false,
      },
    };

    const summary = providerUsageSummary(raw);
    expect(summary).toMatchObject({
      input: 100,
      ordinaryInput: 100,
      inputIncludesCacheRead: false,
      inputIncludesCacheWrite: false,
      cacheRead: 600,
      cacheWrite: 300,
      promptCacheTotal: 1_000,
    });
    expect(providerCacheHitRate(summary)).toBe(60);

    const exported = providerTraceExportPackage(raw, "2026-08-19T00:00:00.000Z");
    expect(exported.usage).toMatchObject({
      inputIncludesCacheRead: false,
      inputIncludesCacheWrite: false,
      ordinaryInput: 100,
      promptCacheTotal: 1_000,
    });
    expect(providerTracePayloadFromExport(exported)).toMatchObject({
      usage: {
        inputIncludesCacheRead: false,
        inputIncludesCacheWrite: false,
        ordinaryInput: 100,
        promptCacheTotal: 1_000,
      },
    });
  });

  it("reports unknown cache usage as n/a instead of a false zero-percent miss", () => {
    expect(providerCacheHitRate({
      input: 800,
      output: 20,
      cacheRead: 0,
      cacheWrite: 0,
      reasoning: 0,
    })).toBeNull();
  });

  it("normalizes DeepSeek prompt cache usage fields in provider traces", () => {
    const summary = providerUsageSummary({
      provider: "openai_chat_completions",
      model: "deepseek-v4-flash",
      usage: {
        prompt_cache_hit_tokens: 16_000,
        prompt_cache_miss_tokens: 7_000,
        completion_tokens: 5,
      },
    });

    expect(summary.input).toBe(23_000);
    expect(summary.cacheRead).toBe(16_000);
    expect(summary.promptCacheTotal).toBe(23_000);
    expect(providerCacheHitRate(summary)).toBe(69.6);
  });

  it("summarizes Anthropic container, sources, refusal category, and native usage", () => {
    const raw: ProviderRawMetadata = {
      provider: "anthropic",
      raw_usage: {
        service_tier: "priority",
        inference_geo: "us",
        cache_creation: {
          ephemeral_5m_input_tokens: 120,
          ephemeral_1h_input_tokens: 340,
        },
        server_tool_use: {
          web_search_requests: 2,
          web_fetch_requests: 1,
        },
      },
      search_sources: [
        { title: "One", url: "https://example.test/one" },
        { title: "Document result" },
      ],
      container: {
        id: "container-1",
        expires_at: "2026-08-16T20:00:00Z",
      },
      refusal: {
        type: "refusal",
        category: "cyber",
        explanation_available: true,
      },
    };

    expect(providerNativeUsageDetails(raw)).toEqual({
      cache5m: 120,
      cache1h: 340,
      webSearchRequests: 2,
      webFetchRequests: 1,
      serviceTier: "priority",
      inferenceGeo: "us",
    });
    expect(providerSearchSourcesSummary(raw)).toBe("2 sources · 1 link");
    expect(providerContainerSummary(raw)).toBe("container-1 · expires 2026-08-16T20:00:00Z");
    expect(providerRefusalSummary(raw)).toBe("declined · cyber");
    expect(hasProviderContainerMetadata(raw.container)).toBe(true);
    expect(hasProviderRefusalMetadata(raw.refusal)).toBe(true);
    expect(hasProviderContainerMetadata({})).toBe(false);
    expect(hasProviderRefusalMetadata({})).toBe(false);
    expect(providerContainerSummary({ container: {} })).toBe("none");
    expect(providerRefusalSummary({ refusal: {} })).toBe("none");
  });

  it("omits empty optional Provider metadata from exports and hydration payloads", () => {
    const raw: ProviderRawMetadata = { provider: "anthropic", container: {}, refusal: {} };
    const exported = providerTraceExportPackage(raw, "2026-08-16T00:00:00.000Z");
    const hydrated = providerTracePayloadFromExport(exported);
    const donePayload = providerTracePayloadFromDone(raw);

    expect(exported).not.toHaveProperty("container");
    expect(exported).not.toHaveProperty("refusal");
    expect(hydrated).not.toHaveProperty("container");
    expect(hydrated).not.toHaveProperty("refusal");
    expect(donePayload).not.toHaveProperty("container");
    expect(donePayload).not.toHaveProperty("refusal");
  });

  it("preserves deferred diagnostic routing metadata in DONE hydration payloads", () => {
    const payload = providerTracePayloadFromDone({
      provider: "openai_responses",
      model: "gpt-5.5",
      trace_id: "run-1:iter:1:provider:1",
      iteration_id: "iter:1",
      call_index: 1,
      diagnostics_deferred: true,
      diagnostics_ref: "provider:run-1:iter:1:provider:1",
      diagnostics_bytes: 4096,
      diagnostics_loaded: false,
    });

    expect(payload).toMatchObject({
      kind: "provider_trace",
      trace_id: "run-1:iter:1:provider:1",
      iteration_id: "iter:1",
      call_index: 1,
      diagnostics_deferred: true,
      diagnostics_ref: "provider:run-1:iter:1:provider:1",
      diagnostics_bytes: 4096,
      diagnostics_loaded: false,
    });
  });

  it("summarizes output phases and response lifecycle without message text", () => {
    const raw: ProviderRawMetadata = {
      provider: "openai_responses",
      model: "gpt-5.4",
      finish_reason: "stop",
      usage: { input_tokens: 10, output_tokens: 3 },
      output_items: [
        { type: "message", index: 0, role: "assistant", phase: "commentary", content_types: ["output_text"] },
        { type: "function_call", index: 1, name: "shell_command", arguments_chars: 120 },
        { type: "message", index: 2, role: "assistant", phase: "final_answer", content_types: ["output_text"] },
      ],
      provider_timeline: [
        { event: "response.created", response_id_hash: "respabc12345" },
        { event: "response.completed", response_id_hash: "respabc12345", output_items_len: 3, usage_present: true },
      ],
      request_summary: { instructions_hash: "prompt123456", tools_hash: "tools123456" },
      safety: { redacted_prompt: true },
    };

    expect(providerOutputPhaseCounts(raw.output_items)).toBe("commentary x1, final_answer x1");
    expect(providerResponseLifecycle(raw.provider_timeline)).toBe("response.created x1 -> response.completed x1 · response respabc12345");
    expect(providerTraceDiagnostics(raw)).toEqual(["final_answer phase present"]);

    const exported = providerTraceExportPackage(raw, "2026-06-28T00:00:00.000Z");
    expect(exported).toMatchObject({
      output_phase_counts: "commentary x1, final_answer x1",
      response_lifecycle: "response.created x1 -> response.completed x1 · response respabc12345",
      diagnostics: ["final_answer phase present"],
    });
    expect(JSON.stringify(exported)).not.toContain("do not leak");
  });

  it("flags tool calls that skip the commentary phase", () => {
    expect(
      providerTraceDiagnostics({
        usage: { input_tokens: 1 },
        output_items: [{ type: "function_call", index: 0, name: "shell_command" }],
        provider_timeline: [{ event: "response.completed" }],
      }),
    ).toContain("tool call without commentary or reasoning context");

    expect(
      providerTraceDiagnostics({
        usage: { input_tokens: 1 },
        output_items: [
          { type: "reasoning", index: 0, has_encrypted_content: true },
          { type: "function_call", index: 1, name: "shell_command" },
        ],
        provider_timeline: [{ event: "response.completed" }],
      }),
    ).not.toContain("tool call without commentary or reasoning context");
  });

  it("flags tool calls emitted after a final answer phase", () => {
    expect(
      providerTraceDiagnostics({
        usage: { input_tokens: 1 },
        output_items: [
          { type: "message", index: 0, role: "assistant", phase: "final_answer" },
          { type: "function_call", index: 1, name: "shell_command" },
        ],
        provider_timeline: [{ event: "response.completed" }],
      }),
    ).toContain("tool call appears after final_answer phase");
  });

  it("summarizes full-history Responses requests that include tool results", () => {
    const raw: ProviderRawMetadata = {
      usage: { input_tokens: 4 },
      finish_reason: "stop",
      output_items: [{ type: "message", index: 0, role: "assistant", phase: "commentary" }],
      provider_timeline: [{ event: "response.completed" }],
      request_summary: {
        instructions_len: 21_465,
        instructions_sent_len: 21_465,
        input_items_len: 6,
        input_items_sent_len: 6,
        input_items_logical_len: 6,
        input_item_counts: { function_call_output: 2 },
      },
    };

    expect(providerInstructionsTransportSummary(raw)).toBe("sent 21465 chars");
    expect(providerSafeRequestPackage(raw).input).toMatchObject({
      input_items_len: 6,
      input_items_sent_len: 6,
      input_items_logical_len: 6,
      input_item_counts: { function_call_output: 2 },
    });
    expect(providerTraceDiagnostics(raw)).toEqual(["trace contract looks healthy"]);
  });

  it("summarizes instructions sent on Responses HTTP requests", () => {
    const raw: ProviderRawMetadata = {
      provider: "openai_responses",
      request_summary: {
        instructions_len: 21465,
        instructions_sent_len: 21465,
      },
    };

    expect(providerInstructionsTransportSummary(raw)).toBe("sent 21465 chars");
    expect(providerSafeRequestPackage(raw).prompt).toMatchObject({
      instructions_len: 21465,
      instructions_sent_len: 21465,
    });
  });

  it("diagnoses loop-bound latency when cache hit rate is already high", () => {
    const raw: ProviderRawMetadata = {
      provider: "openai_responses",
      model: "gpt-5.5",
      usage: {
        input_tokens: 40_000,
        output_tokens: 120,
        cache_read_input_tokens: 36_000,
        prompt_cache_total_tokens: 40_000,
        prompt_cache_hit_rate: 90,
      },
      output_items: [{ type: "message", index: 0, role: "assistant", phase: "final_answer" }],
      provider_timeline: [{ event: "response.completed" }],
      loop_metrics: {
        provider_call_count: 7,
        iteration: 7,
        iteration_limit: 12,
        iteration_hard_limit: 60,
        tool_batch_count: 6,
        tool_call_count: 24,
        elapsed_ms: 185_000,
        dynamic_iteration_budget_enabled: true,
      },
    };

    expect(providerLoopMetricsSummary(raw)).toBe("7 provider calls · iter 7/12/60 · 6 tool batches · 24 tools · 185000ms");
    expect(providerTraceDiagnostics(raw)).toEqual([
      "final_answer phase present",
      "high provider-call count: 7",
      "high tool-batch count: 6",
      "high tool-call count: 24",
      "cache hit is high; latency is likely loop/tool-bound",
      "dynamic iteration window active: 12/60",
    ]);

    const exported = providerTraceExportPackage(raw, "2026-07-02T00:00:00.000Z");
    expect(exported.loop_metrics).toMatchObject({ provider_call_count: 7, tool_call_count: 24 });
    expect(providerTracePayloadFromExport(exported)).toMatchObject({
      loop_metrics: { provider_call_count: 7, tool_call_count: 24 },
    });
  });

  it("summarizes provider request mode and cache-retention flags", () => {
    expect(
      providerRequestModeSummary({
        provider: "openai_responses",
        request_summary: {
          wire_api: "responses",
          request_params: { store: false, prompt_cache_retention: "24h" },
        },
      }),
    ).toBe("responses · retention 24h · store false");

    expect(
      providerRequestModeSummary({
        provider: "openai_responses",
        request_summary: {
          wire_api: "responses",
          request_params: { store: false },
        },
      }),
    ).toBe("responses · retention off · store false");

    expect(
      providerRequestModeSummary({
        provider: "openai_chat_completions",
        request_summary: { wire_api: "chat", request_params: { stream: true } },
      }),
    ).toBe("chat · retention off · store n/a");
  });

  it("treats store=false as the normal Responses HTTP contract", () => {
    expect(providerTraceDiagnostics({
      usage: { input_tokens: 1 },
      output_items: [{ type: "message", index: 0, role: "assistant", phase: "final_answer" }],
      provider_timeline: [{ event: "response.completed" }],
      request_summary: {
        wire_api: "responses",
        prompt_cache_key_present: true,
        request_params: { stream: true, store: false },
      },
    })).toEqual(["final_answer phase present"]);
  });

  it("warns when GPT-like models use Chat Completions instead of Responses", () => {
    expect(
      providerTraceDiagnostics({
        provider: "openai_chat_completions",
        model: "gpt-5.5",
        usage: { input_tokens: 100, output_tokens: 10 },
        output_items: [{ type: "message", index: 0, role: "assistant", phase: "final_answer" }],
        provider_timeline: [{ event: "chat.completed" }],
        request_summary: { wire_api: "chat", model: "gpt-5.5" },
      }),
    ).toContain("GPT-like model is using Chat Completions instead of the Responses wire API");

    expect(
      providerTraceDiagnostics({
        provider: "openai_responses",
        model: "gpt-5.5",
        usage: { input_tokens: 100, output_tokens: 10 },
        output_items: [{ type: "message", index: 0, role: "assistant", phase: "final_answer" }],
        provider_timeline: [{ event: "response.completed" }],
        request_summary: { wire_api: "responses", model: "gpt-5.5" },
      }),
    ).not.toContain("GPT-like model is using Chat Completions instead of the Responses wire API");
  });

  it("flags legacy Responses bridge requests that bypass cache routing", () => {
    expect(
      providerTraceDiagnostics({
        provider: "openai_responses",
        model: "gpt-5.5",
        usage: { input_tokens: 100, output_tokens: 10 },
        output_items: [{ type: "message", index: 0, role: "assistant", phase: "final_answer" }],
        provider_timeline: [{ event: "response.completed" }],
        request_summary: {
          wire_api: "responses",
          model: "gpt-5.5",
          prompt_cache_key_present: false,
          request_params: { stream: true, store: false },
          tools_len: 1,
          tool_names: ["minicode_app"],
        },
      }),
    ).toEqual([
      "final_answer phase present",
      "Responses request missing prompt_cache_key; stable prompt cache routing is disabled",
      "single minicode_app bridge tool detected; current backend tool/cache path may be bypassed",
    ]);
  });

  it("reports stable request scaffolds and incomplete provider boundaries", () => {
    expect(
      providerRequestDiffSummary(
        { instructions_hash: "stable", instructions_full_hash: "full-a", instructions_len: 100 },
        { instructions_hash: "stable", instructions_full_hash: "full-b", instructions_len: 100 },
      ),
    ).toContain("Dynamic prompt changed");

    expect(
      providerRequestDiffSummary(
        { instructions_hash: "stable-a", instructions_full_hash: "full-a", instructions_len: 100 },
        { instructions_hash: "stable-b", instructions_full_hash: "full-b", instructions_len: 120 },
      ),
    ).toContain("Stable prompt changed");

    expect(
      providerRequestDiffSummary(
        {
          instructions_hash: "prompt",
          tools_hash: "tools",
          tool_names: ["shell_command"],
          request_params: { stream: true },
          prompt_cache_key_hash: "cache",
          metadata_keys: ["cwd"],
          input_items_len: 2,
          input_item_counts: { message: 2 },
        },
        {
          instructions_hash: "prompt",
          tools_hash: "tools",
          tool_names: ["shell_command"],
          request_params: { stream: true },
          prompt_cache_key_hash: "cache",
          metadata_keys: ["cwd"],
          input_items_len: 4,
          input_item_counts: { message: 3, function_call_output: 1 },
        },
      ),
    ).toContain("Stable request scaffold; sent input +2");

    const fullHistoryDiff = providerRequestDiffSummary(
      {
        instructions_hash: "prompt",
        instructions_full_hash: "full-a",
        instructions_len: 100,
        tools_hash: "tools",
        request_params: { stream: true, store: false },
        request_param_keys: ["model", "stream", "store"],
        prompt_cache_key_hash: "cache",
        input_items_len: 4,
        input_items_sent_len: 4,
        input_items_logical_len: 4,
      },
      {
        instructions_hash: "prompt",
        instructions_full_hash: "full-b",
        instructions_len: 120,
        tools_hash: "tools",
        request_params: { stream: true, store: false },
        request_param_keys: ["model", "stream", "store"],
        prompt_cache_key_hash: "cache",
        input_items_len: 5,
        input_items_sent_len: 5,
        input_items_logical_len: 5,
      },
    );
    expect(fullHistoryDiff).toContain("Dynamic prompt changed");
    expect(fullHistoryDiff).toContain("Params unchanged");
    expect(fullHistoryDiff).toContain("Input items +1");
    expect(fullHistoryDiff).toContain("Stable request scaffold; sent input +1");

    expect(
      providerRequestDiffSummary(
        {
          request_params: { stream: true, store: true },
          request_param_keys: ["model", "stream", "store"],
          input_items_len: 1,
        },
        {
          request_params: { stream: true },
          request_param_keys: ["model", "stream"],
          input_items_len: 1,
        },
      ),
    ).toContain("Params changed (store; keys -store)");

    expect(
      providerRequestDiffSummary(
        {
          tools_hash: "tools-old",
          tools_len: 1,
          tool_names: ["read_file"],
          tool_schema_hashes: { read_file: "schema-old" },
        },
        {
          tools_hash: "tools-new",
          tools_len: 1,
          tool_names: ["read_file"],
          tool_schema_hashes: { read_file: "schema-new" },
        },
      ),
    ).toContain("Tools changed (schema ~read_file)");

    expect(
      providerRequestDiffSummary(
        { turn_aborted_marker_present: false, input_items_len: 1, input_item_counts: { message: 1 } },
        { turn_aborted_marker_present: true, input_items_len: 2, input_item_counts: { message: 2 } },
      ),
    ).toContain("Abort marker appeared");

    expect(
      providerTraceDiagnostics({
        event_type: "response.error",
        finish_reason: "turn_aborted",
        usage: { input_tokens: 1 },
        output_items: [{ type: "message", index: 0, role: "assistant", phase: "commentary" }],
        provider_timeline: [{ event: "response.error", status: "failed" }],
        request_summary: { turn_aborted_marker_present: true },
      }),
    ).toEqual([
      "provider lifecycle has incomplete/error boundary",
      "turn_aborted marker present in captured input",
      "turn may have been aborted or interrupted",
    ]);
  });

  it("summarizes prompt section diagnostics for inspector display", () => {
    const raw: ProviderRawMetadata = {
      provider: "openai_responses",
      usage: { input_tokens: 10, output_tokens: 4, cache_read_input_tokens: 2 },
      request_summary: {
        instructions_hash: "prompt-hash",
        prompt_section_summary: {
          section_count: 4,
          total_chars: 1200,
          layers: {
            stable: { chars: 800, sections: 1, cache_break_sections: 0 },
            context: { chars: 250, sections: 2, cache_break_sections: 0 },
            volatile: { chars: 150, sections: 1, cache_break_sections: 1 },
          },
          largest_sections: [
            { name: "stable_system", layer: "stable", chars: 800 },
            { name: "workspace_summary", layer: "context", chars: 180 },
          ],
        },
      },
      prompt_cache_diagnostic: {
        reason: "prompt sections changed",
        token_drop: 6000,
        prompt_section_delta: {
          status: "changed",
          added: ["skill_context"],
          removed: ["workspace_summary"],
          changed_sections: [
            { name: "stable_system", changes: ["content"], chars_delta: 30 },
          ],
          layer_char_deltas: { stable: 30, context: -120, volatile: 0 },
        },
      },
    };

    expect(providerPromptSectionSummary(raw.request_summary?.prompt_section_summary)).toBe(
      "4 sections · 1200 chars · stable 800 chars / 1 sections · context 250 chars / 2 sections · volatile 150 chars / 1 sections, 1 cache-break",
    );
    expect(providerPromptLargestSections(raw.request_summary?.prompt_section_summary)).toBe(
      "stable_system (stable, 800 chars) · workspace_summary (context, 180 chars)",
    );
    expect(providerPromptCacheDiagnosticSummary(raw.prompt_cache_diagnostic)).toBe(
      "prompt sections changed · token drop 6000",
    );
    expect(providerPromptSectionDeltaSummary(raw.prompt_cache_diagnostic?.prompt_section_delta)).toEqual({
      overview: "added skill_context · removed workspace_summary · changed 1 section",
      layerSummary: "stable +30 chars · context -120 chars",
      changedSections: "stable_system [content, +30 chars]",
    });
    expect(providerTraceDiagnostics(raw)).toContain("prompt section delta captured");
  });

  it("sanitizes prompt content and secret-like fields from trace exports", () => {
    const raw = {
      provider: "openai_responses",
      model: "gpt-5.5",
      usage: { input_tokens: 1, output_tokens: 1 },
      request_summary: {
        instructions_hash: "prompt-hash",
        instructions_len: 123,
        metadata_keys: ["turn_id", "api_key", "Authorization"],
        instructions: "full system prompt should not export",
        api_key: "secret-key",
      },
      output_items: [
        {
          type: "reasoning",
          index: 0,
          has_encrypted_content: true,
          encrypted_content: "encrypted-value",
          content: "hidden chain",
        },
      ],
      provider_timeline: [
        {
          event: "response.output_text.delta",
          text: "raw streamed text",
          authorization: "Bearer secret",
        },
      ],
      citations: [{
        source: "anthropic:document:abc",
        title: "Architecture notes",
        label: "Pages 2–3",
        range: [2, 3],
        cited_text: "provider cited text should not export",
      } as unknown as NonNullable<ProviderRawMetadata["citations"]>[number]],
      refusal: {
        type: "refusal",
        category: "policy",
        explanation_available: true,
        explanation: "provider explanation should not export",
      } as ProviderRawMetadata["refusal"] & { explanation: string },
      safety: { redacted_prompt: true, has_encrypted_reasoning: true },
    } satisfies ProviderRawMetadata & Record<string, unknown>;

    const exported = providerTraceExportPackage(raw, "2026-06-28T00:00:00.000Z");
    const json = JSON.stringify(exported);

    expect(json).toContain("prompt-hash");
    expect(json).toContain("has_encrypted_content");
    expect(json).not.toContain("full system prompt should not export");
    expect(json).not.toContain("secret-key");
    expect(json).not.toContain("encrypted-value");
    expect(json).not.toContain("hidden chain");
    expect(json).not.toContain("Bearer secret");
    expect(json).not.toContain("provider cited text should not export");
    expect(json).not.toContain("provider explanation should not export");
    expect(exported.refusal).toEqual({
      type: "refusal",
      category: "policy",
      explanation_available: true,
    });
    expect(exported.request_summary?.metadata_keys).toEqual(["turn_id"]);
    expect(sanitizeProviderTraceExportValue({ prompt: "x", safe: 1 })).toEqual({ safe: 1 });
  });

  it("builds safe request JSON and cURL skeletons without replaying prompt content", () => {
    const raw: ProviderRawMetadata = {
      provider: "openai_responses",
      model: "gpt-5.5",
      request_summary: {
        model: "gpt-5.5",
        wire_api: "responses",
        instructions_len: 21459,
        instructions_hash: "prompt-hash",
        tools_len: 2,
        tools_chars: 842,
        tools_hash: "tools-hash",
        tool_names: ["shell_command"],
        tool_schema_hashes: { shell_command: "schema-hash" },
        largest_tools: [{ name: "shell_command", chars: 640 }],
        metadata_keys: ["turn_id", "api_key"],
        input_items_len: 3,
        input_chars: 16_900,
        input_item_counts: { message: 2, function_call_output: 1 },
        largest_input_items: [{ index: 0, type: "message", role: "developer", chars: 16_789 }],
        duplicate_input_content: [{ type: "message", role: "developer", content_hash: "same-hash", count: 2, chars: 33_578 }],
        request_params: {
          stream: true,
          store: false,
          reasoning: { summary: "auto", effort: "high" },
          prompt: "do not copy",
        },
      },
    };

    const safeRequest = providerSafeRequestPackage(raw);
    const curl = providerCurlSkeleton(raw);
    const combined = JSON.stringify(safeRequest) + curl;

    expect(safeRequest.endpoint).toBe("/v1/responses");
    expect(safeRequest.prompt).toMatchObject({ redacted: true, instructions_hash: "prompt-hash" });
    expect(providerLargestToolsSummary(raw.request_summary)).toBe("shell_command (640 chars)");
    expect(providerLargestInputItemsSummary(raw.request_summary)).toBe("#0 message:developer (16789 chars)");
    expect(providerDuplicateInputSummary(raw.request_summary)).toBe("message:developer x2 (33578 chars)");
    expect(safeRequest.tools.tools_chars).toBe(842);
    expect(safeRequest.tools.tool_schema_hashes).toEqual({ shell_command: "schema-hash" });
    expect(safeRequest.tools.largest_tools).toEqual([{ name: "shell_command", chars: 640 }]);
    expect(safeRequest.input.input_chars).toBe(16_900);
    expect(safeRequest.input.largest_input_items).toEqual([{ index: 0, type: "message", role: "developer", chars: 16_789 }]);
    expect(safeRequest.input.duplicate_input_content).toEqual([{ type: "message", role: "developer", content_hash: "same-hash", count: 2, chars: 33_578 }]);
    expect(combined).toContain("<redacted len=21459 hash=prompt-hash>");
    expect(combined).toContain("Bearer $PROVIDER_API_KEY");
    expect(combined).not.toContain("do not copy");
    expect(combined).not.toContain("api_key");
  });

  it("formats provider timeline rows with useful event details and tones", () => {
    const rows = providerTimelineRows([
      { event: "response.output_item.added", item_type: "function_call", name: "shell_command" },
      { event: "response.function_call_arguments.delta", delta_chars: 42 },
      { event: "response.completed", status: "completed", output_items_len: 2, usage_present: true },
      { event: "response.incomplete", finish_reason: "turn_aborted" },
    ]);

    expect(rows[0]).toMatchObject({
      event: "response.output_item.added",
      detail: "item function_call · tool shell_command",
      tone: "muted",
    });
    expect(rows[1].detail).toBe("+42 chars");
    expect(rows[2]).toMatchObject({ detail: "status completed · 2 output items · usage", tone: "accent" });
    expect(rows[3].tone).toBe("warning");
  });

  it("exports multiple provider traces as safe JSONL with request diffs", () => {
    const first: ProviderRawMetadata = {
      provider: "openai_responses",
      model: "gpt-5.5",
      request_summary: { instructions_hash: "p1", tools_hash: "t1", input_items_len: 1 },
    };
    const second: ProviderRawMetadata = {
      provider: "openai_responses",
      model: "gpt-5.5",
      request_summary: { instructions_hash: "p1", tools_hash: "t2", input_items_len: 2 },
    };

    const lines = providerTraceExportJsonl([first, second]).split("\n");

    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[0]).request_diff_summary).toBeUndefined();
    expect(JSON.parse(lines[1]).request_diff_summary).toContain("Tools changed");
  });

  it("converts exported provider trace packages back into inspector payloads", () => {
    const exported = providerTraceExportPackage({
      provider: "openai_responses",
      model: "gpt-5.5",
      finish_reason: "stop",
      usage: { input_tokens: 4, output_tokens: 2 },
      output_items: [{ type: "message", index: 0, phase: "final_answer" }],
      provider_timeline: [{ event: "response.completed", output_items_len: 1 }],
      request_summary: { instructions_hash: "prompt-hash" },
      safety: { redacted_prompt: true },
    });

    expect(providerTracePayloadFromExport(exported)).toMatchObject({
      kind: "provider_trace",
      provider: "openai_responses",
      model: "gpt-5.5",
      finish_reason: "stop",
      request_summary: { instructions_hash: "prompt-hash" },
    });
    expect(providerTracePayloadFromExport({ kind: "unknown" })).toBeNull();
  });
});
