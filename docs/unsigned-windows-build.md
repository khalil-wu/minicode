# Unsigned Windows test installer

For development and manual testing only. The production signing workflow
(\`.github/workflows/release-windows.yml\`) remains mandatory for tag-based
GitHub Releases and the production auto-update feed.

## Build without a certificate

1. Open **Actions → MiniCode Windows unsigned test build → Run workflow**.
2. Select the intended branch and click **Run workflow**.
3. Download the \`minicode-windows-unsigned-test\` artifact ZIP from the completed run.
4. Extract it and verify the installer checksum against \`SHA256SUMS.txt\` before running.

A local Windows build is also available:

\`\`\`powershell
cd frontend
npm ci
cd ../desktop
npm ci
npm run dist:win:unsigned
\`\`\`

Output: \`desktop/release-unsigned/*.exe\`. This build uses a separate
electron-builder configuration that omits the update provider, disables
code signing, and packages an \`UNSIGNED_TEST_BUILD\` marker. The desktop
updater checks the marker and never checks, downloads, or installs updates
in this build.

**Security and distribution boundaries:**

- Unsigned installers do **not** establish publisher identity. Windows SmartScreen
  may warn or block the installer. Do not instruct users to disable security
  protections globally.
- A SHA-256 checksum catches accidental changes but is **not** a substitute
  for a publisher signature: an attacker controlling the download location
  could replace both the installer and checksum.
- The workflow is manual-only and uploads to **Actions artifacts**, not
  GitHub Releases. It must never upload \`latest.yml\` or \`*.blockmap\`
  to the production auto-update feed.
- Unsigned test builds do not use the automatic updater; install subsequent
  versions manually after verifying their provenance.
- For public and automatic updates, keep the separate signed release pipeline
  and use a real trusted signing service/certificate. Do not weaken signature
  verification in that pipeline.
