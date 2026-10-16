# Pinned Source network runtime

This bridge compiles against Codex Source commit
`822e58cc3d666166c7446c5b1ea2e52f5d09594c`. It directly uses Source
`NetworkProxySpec`, `NetworkProxy`, native exec-policy rules and the native
network-denial formatter. Executor permission profiles use Source
`codex_file_system::ExecPermissionProfile` so URI paths retain their executor
path convention.

Build with `desktop/scripts/prepare-network-runtime.py`, an explicit Rust 1.95.0
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
