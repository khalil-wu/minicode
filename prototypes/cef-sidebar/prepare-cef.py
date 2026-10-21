"""Download the pinned SDK only on a disposable GitHub Windows runner."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request


def main():
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("CEF SDK preparation is restricted to Windows GitHub Actions")
    pin = json.loads(Path(__file__).with_name("cef-version.json").read_text())
    staging = Path(os.environ["RUNNER_TEMP"]) / "cef-sidebar-sdk"
    staging.mkdir()
    archive = staging / pin["archive"]
    url = "https://cef-builds.spotifycdn.com/" + urllib.parse.quote(pin["archive"])
    with urllib.request.urlopen(url, timeout=90) as response, archive.open("wb") as out:
        shutil.copyfileobj(response, out)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha1").hexdigest()
    if archive.stat().st_size != pin["bytes"] or digest != pin["sha1"]:
        raise SystemExit("CEF archive differs from the pinned public build index")
    with tarfile.open(archive, "r:bz2") as bundle:
        bundle.extractall(staging, filter="data")
    sdk = staging / pin["archive"].removesuffix(".tar.bz2")
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as env:
        env.write(f"CEF_SDK_ROOT={sdk}\n")
    shutil.copyfile(Path(__file__).with_name("cef-version.json"),
                    Path(sys.argv[1]) / "cef-version.json")
    print(f"Prepared fixed CEF SDK: {sdk}")


if __name__ == "__main__":
    main()
