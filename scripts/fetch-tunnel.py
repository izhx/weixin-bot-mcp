#!/usr/bin/env python3
"""Fetch and verify the official amd64 bundle; never execute downloaded code."""
import argparse
import hashlib
from pathlib import Path
import urllib.request
import zipfile

VERSION = "v0.0.16"
DIGEST = "d60cdba019bce451bcc3a15478cc5b9cb11270b049f5b56ea39a80b517f8b117"
URL = "https://github.com/openai/tunnel-client/releases/download/" + VERSION + "/tunnel-client-" + VERSION + "-linux-amd64.zip"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    cache = root / ".cache"
    cache.mkdir(exist_ok=True)
    archive = args.archive or cache / "tunnel-client.zip"
    if not archive.is_file():
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != DIGEST:
        raise SystemExit("Official tunnel-client archive SHA256 mismatch")
    target = cache / "tunnel"
    target.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        if any("/" in name or "\\" in name or ".." in name for name in bundle.namelist()):
            raise SystemExit("Unexpected archive paths")
        bundle.extractall(target)
        for name in bundle.namelist():
            (target / name).chmod(0o755 if name in {"tunnel-client", "cloudflared"} else 0o644)
    print("Verified official tunnel-client " + VERSION + " (linux-amd64), including companion runtime and licenses")


if __name__ == "__main__":
    main()
