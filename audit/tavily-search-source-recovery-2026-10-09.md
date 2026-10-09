# Dedicated search source recovery — 2026-10-09

The incident reported as `Hosted web search failed ... Responses API error` came from the selected model provider's native search. Tavily was not called by that request. Historical native search had succeeded; the same historical MiniCode client also failed when replayed against the current provider. See [the original error evidence](hosted-search-error-evidence-2026-10-09.md).

## Source selection

The user supplied a Tavily credential to restore search. An authenticated request to the existing official `https://api.tavily.com/search` endpoint returned HTTP 200 and three nonempty search results. The credential was saved through `EnvVault` to the default desktop profile's OS credential store; it is absent from this document, source files, and plaintext configuration.

Previously `WebSearchTool.execute` always preferred declared hosted search. Configuring Tavily could not select it while `custom / gpt-6.1-sol` continued declaring native search support. The tool now selects a configured dedicated source before resolving the conversation model. Without a dedicated credential, hosted search retains its existing behavior. A source failure remains a failure of that source; there is no failure-driven provider switch.

The same credential-presence state controls domain-exclusion schema exposure. Model schema cache fingerprints include the tool's configuration revision, so already warmed direct and nested code-mode directories update after vault publication/removal. The revision contains a boolean, never a credential. Model factory initialization remains deferred.

## Verification

- Official Tavily authentication and search: HTTP 200, three real result URLs.
- Actual `WebSearchTool.execute` for `北京天气`: `provider=tavily`, `is_error=false`, `extraction_status=ok`, real weather.com.cn and nmc.cn URLs and candidate snippets. The test's model factory would fail if invoked; it was not invoked.
- Search snippets remain candidate evidence. For example, a weather snippet can use yesterday's relative date; a real time answer must verify the source date rather than treating the snippet as a fresh observation.
- Unified regression batch: 278 tests passed in 224.54 seconds. It includes native receipt/error preservation, Tavily priority/failure behavior, warm direct/nested schema cache publication and removal, lazy factories, code-mode execution journals, model ownership, subagent startup, and registry cancellation. Agent kernel boundaries, protocol synchronization, and changed Python compilation passed.
- Packaged desktop task `conv_mv0nhji9_xpsegj` on `custom / gpt-6.1-sol`: the actual search result reported `provider=tavily` and eight official documentation URLs. Both official-page fetches succeeded. The model completed a sourced answer comparing domain filters, answer inclusion, and raw-content evidence with a JSON example. No tool/search failure occurred. This confirms model → app-server → search → final UI rendering. A first completion snapshot showed the inactive process still expanded; automatic folding is not counted as passed from that snapshot.
- The user subsequently took over the QA profile and started a news query. That window, conversation, and task were preserved without switching or closing them.

The active conversation model and its reasoning settings are preserved. No frontend layout or user drafts are changed by this search-source correction.
