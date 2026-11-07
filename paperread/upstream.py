"""Read upstream source identity without importing or executing upstream Python."""

import hashlib
import json
from pathlib import Path
import re
import subprocess


def _git(path: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, check=True,
        timeout=15,
    )
    return result.stdout


def check_upstreams(root: Path) -> list[dict[str, str]]:
    lock = json.loads((root / "upstream.lock.json").read_text(encoding="utf-8"))
    if not isinstance(lock, dict) or lock.get("schema_version") != 1:
        raise ValueError("Invalid source lock schema")
    items = lock.get("repositories")
    if not isinstance(items, list) or len(items) != 2:
        raise ValueError("Both pinned upstream repositories are required")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("Invalid source lock entries")
    if not all(isinstance(item.get("name"), str) for item in items):
        raise ValueError("Invalid upstream repository names")
    if {item["name"] for item in items} != {"WeReadGears", "WeChatReading"}:
        raise ValueError("Invalid upstream repository names")
    for item in items:
        if not isinstance(item.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", item["commit"]):
            raise ValueError("Invalid pinned commit")
        if not isinstance(item.get("patch_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["patch_sha256"]):
            raise ValueError("Invalid patch digest")
    checks = []
    for item in items:
        name = item["name"]
        path = root / "vendor" / name
        status = "MISSING"
        if (path / ".git").is_dir():
            try:
                head = _git(path, "rev-parse", "HEAD").decode().strip()
                diff = _git(path, "diff", "--no-ext-diff", "--binary", "HEAD", "--")
                # Include untracked files; they must not silently shadow src modules.
                unknown = _git(path, "ls-files", "--others", "--exclude-standard")
                expected = item["patch_sha256"]
                status = "PASS" if (head == item["commit"] and not unknown and
                    hashlib.sha256(diff).hexdigest() == expected) else "MISMATCH"
            except (OSError, subprocess.SubprocessError):
                status = "ERROR"
        checks.append({"name": name, "commit": item["commit"], "status": status})
    return checks
