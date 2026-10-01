# Windows sandbox runtime

`npm run runtime:windows-sandbox` builds from the pinned public Codex Windows
source with MiniCode-specific accounts, ACL setup, Job Object ownership,
private desktop, Firewall rules and WFP object IDs.

The generated `codex.exe` and `runtime.json` stay out of Git. The packaged app
ships both files under `resources/windows-sandbox/` together with the upstream
Apache-2.0 license and notice in this directory.

The source pin is `rust-v0.158.0-alpha.2.1`, the exact wrapper protocol used
by `backend/sandbox/windows_native.py`. The build verifies the downloaded
archive SHA-256 and records all output hashes. Packaging rejects an upstream
binary without the MiniCode identity manifest.
