# Pinned Source network runtime

This bridge compiles against Codex Source commit
`970111f6cb62cc02fe36a82ea7d71b37a2889c97`. It directly uses Source
`NetworkProxySpec`, `NetworkProxy`, native exec-policy rules and the native
network-denial formatter. Executor permission profiles use Source
`codex_file_system::ExecPermissionProfile` so URI paths retain their executor
path convention.

The same process transport also exposes two independent media meters:
`media/audioTokens({audioUrl}) -> {tokens}` calls the pinned Source audio
estimator; `media/originalImageBytes({imageUrl}) -> {bytes}` uses the exact
private history-estimator ranges checked by the builder (`bytes` may be null).
They use Source's Symphonia and image crates, with no FFmpeg/PyAV dependency.
These calls do not create a network execution or change policy, routing or
lease ownership. Python can use the existing synchronous JSON invocation;
there is no separate media service or persistent process.

The browser transport adapter also uses Source's dedicated loopback listeners
and checked TCP connector. `network/startBrowser` publishes the reserved proxy
endpoint; `network/setBrowserTargets` replaces explicit host/port/protocol
grants through Source's mandatory review decider. It keeps local binding false,
so granting one preview endpoint does not authorize other private services or
public DNS names rebound onto private addresses. Releasing its execution closes
the actual listener and its connections. This adapter does not configure
Chromium Sessions or replace their system/PAC routing by itself.

Build with `desktop/scripts/prepare-network-runtime.py`, an explicit Rust 1.99.1
Cargo executable, the pinned Source checkout, an isolated build directory and
an output directory. The output contains the executable, Source provenance,
licenses and notices. Package that output under
`backend/network/resources/<target>/` using the same target names as execpolicy.

One native execution has one live routing owner. Python borrowers clone that
owner; the last borrower releases the native execution. Local actors drain
native audit delivery after their original process tree closes. The command
owner retains its borrower through review completion and any already accepted
permanent amendment.

Remote execution sends Source's portable remote launch config to the actual
executor, which selects listeners and enforces its sandbox. Local routing
currently requires the elevated Windows sandbox, Source shared SID ingress,
and matching explicitly provisioned Windows proxy settings. Proxy environment
variables alone do not provide that enforcement.

The Windows adapter checks `sandbox network-ready` without provisioning. A
pending prerequisite comes from the actual prepared Source proxy and the
trusted native runtime owner. Only the explicit desktop setup action consumes
its `proxy_ports` and `allow_local_binding`. Workload launches pass
`--require-existing-sandbox`, preventing Source's automatic account/firewall
setup path. Linux/macOS local routing and unprovisioned Windows traffic remain
outside the verified local network matrix.
