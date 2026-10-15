#!/usr/bin/env bash
set -euo pipefail

probe_bwrap() {
  bwrap --unshare-user --unshare-net --ro-bind / / --proc /proc --dev /dev --perms 000 --tmpfs /tmp --remount-ro /tmp -- true
}

if probe_bwrap 2> "$RUNNER_TEMP/minicode-bwrap-probe.log"; then
  exit 0
fi

cat "$RUNNER_TEMP/minicode-bwrap-probe.log"
test "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)" = 1
# Grant user namespaces to the one launcher; Bubblewrap still owns the
# filesystem and network isolation used by backend and native editor tests.
sudo tee /etc/apparmor.d/minicode-ci-bwrap > /dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile minicode-ci-bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
PROFILE
sudo apparmor_parser -r /etc/apparmor.d/minicode-ci-bwrap
probe_bwrap
