"""Fetch exact source commits and apply the reviewed quarantine patch.

No upstream Python is imported, no requirements are installed, no login occurs.
Existing checkouts are verified and never reset or overwritten.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def git(path: Path, *args: str, input_data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(path), *args], input=input_data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=120,
    ).stdout


def bootstrap(root: Path = ROOT) -> None:
    lock = json.loads((root / "upstream.lock.json").read_text(encoding="utf-8"))
    vendor = root / "vendor"
    vendor.mkdir(exist_ok=True)
    for item in lock["repositories"]:
        path = vendor / item["name"]
        patch = (root / item["patch"]).read_bytes() if item.get("patch") else b""
        expected = hashlib.sha256(patch).hexdigest()
        if expected != item["patch_sha256"]:
            raise RuntimeError("Patch checksum mismatch; stopped")
        if not path.exists():
            # Network access here is limited to public source retrieval by git.
            git(vendor, "clone", "--no-checkout", item["url"], item["name"])
            # Preserve the pinned source's mixed LF/CRLF blobs exactly on Windows.
            git(path, "config", "core.autocrlf", "false")
            git(path, "checkout", "--detach", item["commit"])
        if git(path, "rev-parse", "HEAD").decode().strip() != item["commit"]:
            raise RuntimeError("Upstream commit mismatch; existing checkout preserved")
        unknown = git(path, "ls-files", "--others", "--exclude-standard")
        if unknown:
            raise RuntimeError("Untracked upstream files found; existing checkout preserved")
        diff = git(path, "diff", "--no-ext-diff", "--binary", "HEAD", "--")
        if diff == patch:
            print(item["name"] + ": PASS (pinned and isolated)")
            continue
        if diff:
            raise RuntimeError("Upstream modifications differ; existing checkout preserved")
        if patch:
            git(path, "apply", "--check", "-", input_data=patch)
            git(path, "apply", "-", input_data=patch)
        if git(path, "diff", "--no-ext-diff", "--binary", "HEAD", "--") != patch:
            raise RuntimeError("Post-patch verification failed; stopped")
        print(item["name"] + ": PASS (pinned and isolated)")


if __name__ == "__main__":
    try:
        bootstrap()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        print("BLOCKED: upstream setup failed; no upstream runtime was started", file=sys.stderr)
        sys.exit(1)
