# Original-executor worktree lifecycle

The remote worktree routes now execute through the request's captured executor. Local routes retain their existing manager and snapshot store. No user windows, profiles, drafts or existing local snapshots were migrated or modified.

## Implementation

`executor_worktree.py` validates original filesystem paths, registration, the primary real `.git` directory and the linked metadata/backlink chain through `registered_worktree_metadata`. The reference is `.tmp/codex-src/codex-rs/git-utils/src/worktree.rs`, commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`.

GitPanel already sends `workspace.set`. Its remote backend now creates a new conversation and accepts that conversation's own target cwd through `ThreadEnvironments.retarget`, cloning the captured original registration rather than borrowing an environment ID. Repeated acceptance retains an existing matching owner. Previous selections and conversations stay intact. The repository, composite ownership keys and renderer snapshot preserve execution URIs; local runtime getters do not turn them into controller paths. Root's run preparation requires the original accepted owner.

Switch validates registration and reports actual Git metadata and tracked file count, without the deletion inventory scan. Remove rejects the current checkout, unrelated/non-isolated paths, incomplete inventories, nested Git markers and dirty state without force. A GUI action requires write authority at its original cwd; only after canonical registration/isolated-parent verification does its captured copy receive temporary write permission for that parent. Original thread/editor/shell permissions remain unchanged and explicit denies remain. The restricted Windows action path still has the failures below.

Snapshots use an independent target Git index. Tracked, untracked and ignored files enter the tree; regular blobs are replaced with `hash-object --no-filters` content while preserving their Git modes. This retains actual bytes despite newline and clean filters. Structured output comes from the original runner stream, not its model-visible 50-KiB tail. Metadata lives in the snapshot commit message and `refs/minicode/wt-snapshots/`, with 50-record/30-day retention. Remote snapshots do not use the controller metadata store. Failed removal retains the real ref and checkout.

Restore creates a detached checkout from the real snapshot, restricts destination selection to its original parent and writes original regular blobs through the original filesystem. Symbolic-link restoration is requested from Git, but the Windows privilege-dependent case remains unverified.

Accepted transactions retain their original service until they settle, including repeated cancellation and HTTP disconnect. Git commands use the original command runner. Temporary index cleanup has a separate original runner/lease, including the no-process case; it waits for original Git cleanup and retains filesystem failures in the existing background cleanup owner.

## Validation

Entry: `desktop/python-runtime/python.exe -u output/ci-repair-20261015/pytest-python311.py`. Independent Source peer: `C:/Program Files/WindowsApps/OpenAI.Codex_26.1007.9446.0_x64__2p2nqsd0c76g0/app/resources/codex.exe`, running `exec-server --listen stdio --concurrent-requests 4` in owned disposable fixtures.

- Final worktree batch: **10 passed / 2 failed / 1 skipped**, zero test errors. The failures remain failures.
- Actual passed paths: LF/CRLF/binary/ignored-byte snapshot and restore; Git executable mode; complete 1,024-file output beyond the runner tail; unchanged staging after failed Git commands; retained snapshot after failed removal; current/unregistered/embedded/read-only rejection; repeated cancellation; and original callback/lease retention after cleanup failure.
- Real WS: replacing the registry object under the same ID does not replace the activation owner. New conversation, next run preparation, cwd and subsequent file writes use the original registration. Protected `.git`/outside-root reads remain rejected. Removing the accepted owner makes REST fail without connecting the replacement.
- Same engine direct local transport: raw-byte snapshot/restore passed. Existing local history/worktree tests separately passed **31/31**; preceding local workspace creation/composite owner tests passed **17/17**.
- GitPanel tests: **11/11**, including visible and hidden unsaved draft retention. No frontend production worktree code changed.
- Real symbolic-link case: Windows **1314 privilege skip**. Mode recovery passed independently. No ACL, account or privilege was changed to manufacture success.

## Remaining restricted Windows failures

In the original Unicode pytest tree, Root READ plus exact target WRITE still produced original Source `fs/writeFile` OS error 5. A read-only Git action also failed to read cwd. Ordinary-directory and worktree-directory probes reproduced the write failure; the same original Source user could still read/write through its existing unrestricted fixture scope.

An owned purely ASCII target with the identical restricted shape successfully wrote and reported actual cwd. This isolates a non-ASCII restricted-path boundary, not complete worktree success. The two unchanged formal tests were also rerun with a new ASCII pytest base. Both still failed: staging could not create linked `index.lock`, and the GUI action received the installed Source rejection that unelevated restricted-token mode cannot enforce split writable root sets. Restricted commit and GUI removal/restore are therefore **not verified or complete**.

Direct restricted filesystem validation independently accepted legitimate linked metadata and rejected a modified backlink with `PermissionError`; that is not a successful restricted commit. Exact wire policies, Unicode/ASCII failures, diagnostics, earlier genuine failures and source freeze are indexed by `output/executor-worktree-20261016/delivery.json`. Diagnostic observation collectors are excluded from feature pass counts. Root owns remaining Windows policy review, packaging and integration.
