# Source agent roles and model request settings

Reference checkout: `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

This batch uses `codex-rs/agent-roles/src/{loader,agent_role_config}.rs`, `core/src/agent/{role,child_config}.rs`, `core/src/agent/control/spawn.rs`, `core/src/client.rs`, `protocol/src/openai_models.rs`, and `codex-api/src/common.rs`. Model support is extracted from this checkout's `models-manager/models.json` into the packaged `backend/llm/source-model-request-capabilities.json`; runtime code does not read the reference checkout.

## Implemented consumers

| Source contract | MiniCode consumer |
| --- | --- |
| Default / explorer / worker; configured TOML role precedence | Model-facing Task schema and captured config role discovery. Explorer has no invented read-only prompt or permission restriction. |
| Layered declarations and recursive TOML discovery | User/project active config folders; relative config files and Skill paths resolve against the owning config/file directory. Higher layers inherit missing role metadata. Malformed definitions produce the Source-style diagnostic and are not advertised. |
| Bounded overrides | Developer instructions enter a real developer message. Model and effort follow requested-selection validation, then role overrides. Only the five Source feature reductions apply. Parent approval, sandbox, filesystem authority, and tool restrictions remain the ceiling. |
| Skill restrictions | Child-owned Skill discovery snapshots filter path/name selectors, bundled Skills and plugin Skills. Refresh retains the child restrictions. `include_instructions=false` suppresses automatic Skill instructions/catalog without removing real historical evidence. |
| Personality | The actual model prompt consumes the role's personality; `none` removes the baked Personality section. |
| V1 / V2 full-history role inheritance | Feature override and captured model `multi_agent_version` choose the Source rule. V1 rejects an explicit role with full history; V2 can apply an explicit role. A configured V2 default is remembered for cold recovery. |
| Cold role recovery | Stored role identity is reapplied from the current TOML file with the current parent's runtime permission ceiling. The old role snapshot remains audit data. |
| Model reasoning summary and verbosity | Real OpenAI Responses and Chat request builders consume the typed request policy. Summary is sent only through Responses; unsupported declared model options are omitted. Responses verbosity coexists with structured-output format. |
| Root-owned service tier | A role cannot select a higher routing tier. The root's selected tier replaces/clears it; existing Source child transports read the current root selection and filter by the child's advertised tiers. `default` is omitted; `flex` follows the Source exception. |
| Child client ownership | Source OpenAI children own adapter/turn state. Pure inheritance retains the invoked step's exact settings instead of reselecting from a refreshed catalog. |
| Structured final output | `ModelRequestPolicy.final_output_json_schema` reaches the real streaming/simple requests. Both transports use `codex_output_schema`; the typed strict flag supports ordinary strict output and Guardian's non-strict output. |
| Guardian cache identity | A trusted typed cache override changes the request's cache key while the private session/thread identities stay intact. Role TOML cannot inject this field. |
| Child approvals | The bridge routes parent conversation/turn/message identity and namespaces the request id. Permissions, stdin action, digest, policy identity and child ownership stay unchanged; the decision returns to the child. Each child AgentSession owns its own grant store. |

Legacy Markdown/YAML files and the old general-purpose / explore / plan roles remain available to explicit runtime callers and historical recovery. They are excluded from the default Source model role surface. A legacy Markdown definition named default / explorer / worker uses an explicit `legacy:<name>` runtime alias; old records retain their original public agent type while recovery selects the old definition internally.

No frontend fonts, typography or new controls are part of this batch. Source roles no longer receive MiniCode's invented concise-report user prompt.

## Validation record

Earlier runs are retained in `output/source-roles-20261014.xml` and the focused diagnostic XML. They include failures and must not be counted as successful validation. The final result is reconstructed by case identity from `output/source-roles-20261014-scope.xml`, `output/source-roles-20261014-resume.xml`, and `output/source-roles-20261014-regression.xml` in `output/source-roles-20261014-results.json`: **335 distinct cases passed, no failing or skipped cases in their last runs**.

The scope run had one five-second initial completion wait expire in an existing teammate recovery fixture under concurrent Windows I/O. Both variants passed in the isolated final run without changing the production timeout or the fixture's wait. Earlier failure evidence remains in its XML. Repeated runs are counted once by case identity.

The initial broad scope stalled because an older Responses fixture injected its controlled HTTP transport only into the parent adapter. The new child-owned adapter attempted that fixture's `.invalid` endpoint. The fixture now injects controlled transport through the real adapter factory for both parent and child; production connection retry behavior is unchanged.

Remaining distinct harness work includes the complete V2 delegation/tool namespace and default execution profiles, tree-wide thread limits/depth, nickname allocation, and Codex's unknown-role-model fallback metadata. This document does not claim those are implemented by this role/config batch.
