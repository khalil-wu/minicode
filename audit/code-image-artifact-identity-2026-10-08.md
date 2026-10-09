# Forwarded code-cell images retain their artifact identity

## Observed failure

In the isolated task `conv_muzn6f8q_5k1o0y`, profile `output/playwright/projection-chain-20261008/qa-profile-1791469894200`, the browser screenshot succeeded. The browser saved its PNG as one owner-scoped artifact. `tool_exec` forwarded the same image to the model, then `_present_result` saved it again with source `tool_exec.image`, producing a second artifact ID approximately 89 ms later. The independently audited payloads had the same owner and identical PNG bytes. The final Markdown referred to the original browser artifact while the automatic image card referred to the wrapper artifact, so a frontend comparison by real artifact ID correctly saw two different records.

## Corrected contract

1. A real leaf `ToolResult` enters `CodeExecutionRuntime._dispatch`. A single image may inherit that result's actual artifact ID; explicit per-image IDs are preserved. A multi-image result's one top-level ID is not assigned indiscriminately to every image.
2. The cell retains only the provenance tuples it actually received: artifact ID, media type, and exact base64 image content. This state belongs to the cell and is cleared when it finishes. It is not a global image cache or an artifact-library scan.
3. The VM's `image(block)` emitter retains the block's ID. When output returns to Python, that ID survives only if the exact tuple appeared in a real leaf result in this cell. Script-created IDs and altered image content do not borrow a leaf's identity.
4. Selected image output and completed cell receipts carry the verified provenance. `_present_result` checks the claimed artifact through existing `ArtifactStore.get_meta/get`, supplying the current conversation and artifact-owner workspace. It reuses the original ID only when type, media type, owner, and full stored content match.
5. An actual new image without that source identity is saved as its own artifact. Two independent original artifacts retain their separate IDs even if their PNG bytes happen to match. No size-based or pixel-similarity merging is performed.
6. The published `artifact.preview` and the selected model image use the same resulting ID. Frontend Markdown/automatic-card deduplication can therefore compare actual identities. Historical records without an explicit real alias are not rewritten.

Production files: `backend/agent/code_execution.py`, `backend/agent/code_execution_vm.js`, and `backend/tools/code_execution.py`. Browser navigation, screenshot pixels, and Task/parallel admission were not changed in this follow-up.

## Verification

```text
python -m pytest backend/tests/test_code_execution.py backend/tests/test_code_audio.py backend/tests/test_browser_control_tool.py backend/tests/test_browser_owned_preview_admission.py -q -o addopts=''
```

**68 passed in 38.17 seconds**, no skips or warnings. `git diff --check` passed.

New real-runtime tests run leaf tools through QueryEngine and the actual JavaScript VM. They prove that two independently saved identical PNGs retain their two original IDs, that no wrapper duplicates are written, and that owner-mismatched, workspace-mismatched, altered-byte, or script-invented identity claims cannot reuse the original artifact. Existing tests still cover selection of new image output, audio behavior, browser screenshot validation, and the preview admission chain.

The parent task will repeat the packaged screenshot → code image forwarding → final Markdown path. This unit/integration batch does not itself claim that the newly packaged frontend has been visually accepted.
