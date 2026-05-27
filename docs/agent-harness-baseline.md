# Agent Harness Baseline

This document captures the agent-loop behaviors MiniCode should preserve as it evolves. It is based on the same harness ideas used by Claude Code and Codex-style local agents: keep the model creative, but put deterministic policy gates around the moments where user trust and cost are most at risk.

Sources:

- Claude Code hooks reference: https://code.claude.com/docs/en/hooks
- Claude Code hooks guide: https://code.claude.com/docs/en/hooks-guide
- OpenAI Codex CLI getting started: https://help.openai.com/en/articles/11096431-openai-codex-cli-getting-tarted

## Lifecycle Gates

MiniCode should treat the loop as lifecycle events, not as one giant prompt.

| Gate | Purpose | MiniCode owner |
| --- | --- | --- |
| Prompt submit | Normalize intent, attach workspace context, block unsafe prompt injection, title sessions. | `ContextBuilder`, future prompt-submit policy |
| Pre tool use | Check permissions, path policy, duplicate calls, destructive commands, sandbox boundaries. | `PermissionChecker`, `AgentState.repeated_call_guard_reason` |
| Tool execution | Run safe read tools concurrently, serialize mutating tools, record artifacts and previews. | `loop._execute_tool_batch`, `ToolResult.to_context_string` |
| Post tool use | Feed tool feedback back to the model, not just the UI. Preserve enough output for final synthesis. | `ContextBuilder.append_tool_result`, `AgentState.tool_calls` |
| Stop | Before `done`, reject empty, hedged, incomplete, or "want me to continue?" replies when tool results already exist. | `DefaultGroundedReplyPolicy` |
| Compact | Compact before the model is starved, keep recent tool outputs and task state. | `ContextBuilder` compaction pipeline |

## Non-Negotiable Product Rules

1. A successful tool result must be usable by the final answer.
   Store `ToolResult.to_context_string()` in both model context and agent state, so artifact previews are available to grounding and reflection.

2. If the user asks for realtime information, the agent must either answer from fresh tool results or clearly state the exact missing source after trying. It must not ask for permission to continue after it already searched.

3. Stop quality gates should be domain-agnostic.
   Domain extractors are allowed only as cheap fast paths. The default path is: detect bad final draft, synthesize from tool results, then emit the final answer.

4. The UI should show useful externalized progress only.
   Internal loop markers such as "choosing next step" belong in debug/timeline views, not in the main chat transcript.

5. Tool repetition is feedback, not a crash.
   Duplicate and repeated calls should be blocked before execution with a reason the model can use to change strategy.

6. Cost controls are part of correctness.
   Do not keep calling the model after fatal provider errors, exhausted balance, or blocked gateway responses. Do not replay large artifacts into context when a preview or targeted read is enough.

7. Attachments must pass through a native-input policy.
   Images and PDFs may be sent as provider-native multimodal input when the active API format supports them and the file is size-safe. Markdown, source code, DOCX, PPTX, XLSX, and ZIP should be parsed, indexed, and referenced through artifacts/RAG by default instead of being replayed into every model call.

## Attachment Input Policy

MiniCode should treat uploaded files as durable artifacts first and provider-native payloads second.

| File kind | Default handling | Why |
| --- | --- | --- |
| Images | Keep base64 and send as native image input when the model supports vision. | Visual understanding needs pixels; artifact text is only metadata. |
| PDF | Parse and index text; also send native PDF for OpenAI Responses or Anthropic when size-safe. | Native layout understanding is useful, but extracted text is the fallback and recall path. |
| Markdown / TXT / code | Parse as text, index, and reference by `artifact_id`/`doc_id`. | Sending raw text every turn wastes context; tools can fetch exact sections. |
| DOCX / PPTX / XLSX | Extract structured text/slides/tables, index, and reference by artifact. | Provider support varies; parsed structure is cheaper and more deterministic. |
| ZIP | List contents and selectively parse small supported members. | Raw archives are unsafe and usually too noisy for model-native input. |

This is the practical overlap between Codex/Claude Code style agents and provider APIs: let the model use native multimodal features where they add real signal, but keep large reusable documents behind artifacts and retrieval.

## Stop Quality Gate Shape

Bad final drafts include:

- empty replies
- "I will check" after tools already ran
- "I found a source but could not extract values" when tool output contains concrete facts
- "if you want, I can continue"
- "I cannot browse" after a successful web tool result

The gate should:

1. inspect recent successful tool results
2. run deterministic fact extractors when available
3. otherwise ask the model for one concise grounded synthesis
4. emit the synthesized answer instead of the bad draft when the draft was buffered

## Future Work

- Add a first-class hook registry mirroring the lifecycle gates above.
- Add prompt-submit injection-defense policy.
- Add post-tool validators for edit commands, test commands, and browser checks.
- Add a compact quality gate that preserves task state, file edits, and unresolved questions.
- Add UI affordances for "blocked by policy, using previous result" so repeated-tool protection feels helpful rather than broken.
