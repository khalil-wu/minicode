# Windows release signing and updates

`.github/workflows/release-windows.yml` builds the Windows installer for every
`v*` tag after the full CI validation. A code signing certificate is optional.

## What the installer job builds

`npm run dist:win` compiles the pinned native runtimes before packaging: the
execution policy and the Windows sandbox with Rust 1.95.0, the network runtime
with Rust 1.99.1 from the pinned Codex commit checked out at `.tmp/codex-src`.
The workflow installs both toolchains, checks out that commit, prefetches the
crates the sandbox build compiles offline, and caches the cargo registry and
target directories between runs. Without these the job fails inside
`Build installer` within seconds.

## Signed releases

Configure the repository secrets `WINDOWS_CERTIFICATE_BASE64` (the `.pfx`/`.p12`
as base64) and `WINDOWS_CERTIFICATE_PASSWORD`. The workflow signs the installer,
verifies the Authenticode signature is `Valid`, and electron-updater keeps its
default policy: an update must be signed by the same publisher.

## Unsigned releases

Without those secrets the same workflow still produces the installer, together
with `latest.yml`, the `.blockmap` and `SHA256SUMS.txt`, and publishes them to
the GitHub release. `desktop/release-build.config.cjs` builds this variant with
`verifyUpdateCodeSignature: false` (so `app-update.yml` carries no
`publisherName`) and packages an `UNSIGNED_BUILD` marker.

What this means:

- Windows SmartScreen may warn or block the installer because it carries no
  publisher identity. Users verify the download against `SHA256SUMS.txt`.
- Installed copies still update. The desktop updater sees the marker, skips
  Authenticode verification by policy, and relies on the HTTPS feed plus the
  sha512 that electron-updater checks against `latest.yml`. The Help menu shows
  "Unsigned build" next to the update actions.
- Whoever controls the feed controls the updates; this is the trade-off of an
  unsigned release. Adding a certificate later switches both the build and the
  verification policy back without any other change.

## Update feed

The repository variable `MINICODE_UPDATE_FEED_URL` (an HTTPS URL) is baked into
the build. When it is not set the workflow uses
`https://github.com/<owner>/<repo>/releases/latest/download`, which GitHub
redirects to the assets of the latest published release.

## Automatic and manual updates

- Settings → General → "Download updates automatically": when on, a found
  update is downloaded in the background; when off (default), the Help menu
  offers "Download Update".
- Installing is always explicit: Help → "Install Update and Restart" runs the
  update preflight (running turns, unsaved editors, terminals, downloads) and
  installs only when nothing would be lost. The previous version is backed up
  and restored automatically if the new one fails its first startup.
