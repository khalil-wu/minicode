# Executor environment setup and Git delivery

The existing environment and Git controls now use the selected executor's status,
tools, files, authentication and original command owner. Status returns the actual
environment, conversation, transport session and registration identity. The remote
POST requires that status registration; a detached/rebound replacement registered
under the same environment ID rejects the old status before mutation.

The two frontend panels retain the GET's conversation/workspace owner and send
the returned environment/registration on POST. Confirmation, streamed output and
late completion cannot affect another conversation at the same cwd. A successful
old commit cannot clear the new owner's unsaved draft. Workspace-keyed mounting
preserves the existing project draft storage when moving between workspaces.
Target `github.remote` status displays the target's explanation and does not send
the user to the controller's GitHub connection settings. Styling and controls were
not redesigned.

Commands use the captured executor policy and the existing
`ExecutorCommandRunner`. Git commit messages/PR bodies are UTF-8 files in the
target's temporary directory; Source has no independent process-stdin EOF method,
so no EOF behavior is invented. File cleanup belongs after process/network close
and before releasing the original lease. Accepted file writes retain their own
task; cancellation waits for that original write to settle before removing its
original URI. Callback failures preserve the original exception and pending lease.
Concurrent cleanup is serialized; late callback handoff is not dropped.

Actual Source probes established that the Windows Rust process implementation
already launches `.cmd` with native argument escaping. Manual `cmd /c` wrapping
returned a quoted command-not-found error; a PowerShell wrapper subsequently
interpreted bare `&`, `|` and `>` as command syntax. Both failed attempts remain in
the logs. Production now sends the original argv to Source. The direct probe and
regression preserved a batch path containing spaces/apostrophes, Chinese, spaces,
apostrophes and literal metacharacters in arguments, with exit code 37 intact.

The installed Windows Codex exec-server was explicitly selected by
`MINICODE_SOURCE_EXECUTOR`; fixtures used private temporary homes/workspaces and
controller shadow files. No external SSH/WSL host, registry network, system setup
or account provisioning was used. npm used empty dependencies, offline settings
and a workspace-local cache. Tests verified actual postinstall marker files and a
working target venv, target Git init/branch, pre-staged commit, exact multi-line
Chinese commit body, original target temporary-file removal and a local bare push.
The controller view was untouched.

The write-cancellation gate is an explicitly owned pass-through RPC peer. It
accepts the original `fs/writeFile` envelope, blocks before forwarding it to the
real Source server, then forwards the original bytes after release. It is not
claimed to instrument the installed Source binary internally. Actual TCP close
must enter server cleanup while that write is blocked; the original lease remains
held. After release, real Source writes and removes the same URI without starting
the canceled commit or connecting to a replacement registration.

Focused evidence is in `output/executor-workbench-20261016/`:

- Frontend `frontend-workbench-batch01.xml`: **16 passed**, no failures/skips.
  The console-reporting wrapper hit a GBK encoding error after Vitest exited 0;
  the original UTF-8 log and official JUnit confirm the test results.
- `source-workbench-batch01.xml`: 6 nodes, **3 passed / 3 failed**. npm failed
  before executing the install script; the initial callback test incorrectly
  expected a result instead of the preserved original exception.
- `source-workbench-batch02.xml`: 4 nodes, **2 passed / 2 failed**. Callback
  failure/retry and concurrent cleanup passed; original batch-command failures
  retained their real exit code 1.
- `source-workbench-batch03.xml`: 4 nodes, **1 passed / 3 failed**. Git passed;
  venv exceeded the newly written fixture's default 5-second HTTP read budget,
  streaming cancellation failed to await cleanup, and literal cmd arguments were
  altered by the shell wrapper.
- `source-direct-argv-probe.json`: diagnostic proof of original Source argv,
  literal argument identity, exit 37 and actual npm version. Not an added test count.
- `source-workbench-batch04.xml`: 4 nodes, **4 passed**, no failures/skips.
  It rechecks installs, actual HTTP disconnect, literal argv/37, and the original
  write/lease barrier. Only the venv test's HTTP client read budget became 30
  seconds; production timeouts and the 15-second cleanup acceptance were unchanged.

Latest focused results are **8 unique Source nodes passed** across these retained
batches, not a single uninterrupted 8-node rerun. Callback fault injection used a
real Source resource and original lease; it is not a physical remote-network-drop
acceptance claim. Uvicorn/websockets warnings remain.

After these focused runs, Root changed `target_programs` to use CLM-compatible
PowerShell hashtables / `Get-Command` / `ConvertTo-Json` and target environment
variables, replacing blocked static .NET calls. Therefore these results precede
that final discovery change. Root's final integrated backend batch is the
authority for the final combined code; this report does not reuse the focused
passes as proof of that later input snapshot. Final frontend build/desktop review
is likewise recorded by Root separately.

Root subsequently fixed another concrete stream lifetime boundary: a disconnected
consumer could leave the producer blocked on the bounded output queue.
`test_source_setup_full_output_queue_closes_original_process_pump_and_lease` was
added for the final integrated batch. It consumes one real body-iterator item,
pauses until the actual 16-item queue is full from a real noisy Source install,
then closes the iterator and checks the original process, pump and lease.
This ninth current source node was not run in the earlier focused batches; Root's
final batch must supply its result. It is not counted as an earlier pass.
