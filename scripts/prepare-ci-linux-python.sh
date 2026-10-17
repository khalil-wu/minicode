#!/usr/bin/env bash
set -euo pipefail

# This portable build was executed on Ubuntu 20.04/glibc 2.31. Do not use
# setup-python's newer hosted-runner toolcache inside the baseline container.
python_root="$RUNNER_TEMP/minicode-python"
archive="$RUNNER_TEMP/cpython-3.11.17-linux-x86_64.tar.gz"
mkdir -p "$python_root"
curl --fail --location --silent --show-error \
  'https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.11.17%2B20261013-x86_64-unknown-linux-gnu-install_only.tar.gz' \
  --output "$archive"
printf '%s  %s\n' '846bf682e2b67393f2affa5e5c0994e446abc3a7cc5bb699890c36346823644c' "$archive" | sha256sum --check -
tar -xzf "$archive" -C "$python_root"
printf '%s\n' "$python_root/python/bin" >> "$GITHUB_PATH"
"$python_root/python/bin/python3" --version
