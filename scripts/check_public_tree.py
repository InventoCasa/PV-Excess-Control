#!/usr/bin/env python3
"""Reject private material in index, tracked working files and new commits.

This is a project boundary check, not a complete secret scanner. It reports only
rule names and file identifiers, never matched contents. Review remains required.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath

PUBLIC_ROOT_FILES = {
    ".gitignore",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "README.de.md",
    "hacs.json",
    "requirements_test.txt",
    "setup.cfg",
}
PUBLIC_SOURCE_SUFFIXES = {
    ".github": {".yml", ".yaml"},
    "custom_components": {
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".js",
        ".css",
        ".svg",
        ".png",
    },
    "requirements-ci": {".txt"},
    "scripts": {".py", ".txt"},
    "tests": {".py", ".json", ".yaml", ".yml"},
}
PRIVATE_PARTS = {"private", "private-ops", "backups", "secrets", "data"}
PATTERNS = {
    "internal host address": re.compile(
        rb"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])"
    ),
    "private workstation path": re.compile(
        rb"/(?:home|Users)/[A-Za-z0-9_.-]+/|[A-Z]:\\Users\\", re.IGNORECASE
    ),
    "JWT literal": re.compile(
        rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ),
    "private key": re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "provider token": re.compile(
        rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})"
    ),
    "credential literal": re.compile(
        rb"(?im)^\s*[\"']?(?:[A-Za-z_]*_)?(?:token|password|secret|api_key)[\"']?\s*[:=]\s*[\"'][^\"'\r\n]{16,}[\"']"
    ),
}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL
    )


def documentation_policy(*, verify_index: bool) -> frozenset[str]:
    """Load this checker's reviewed policy; local checks require its staged copy.

    Historical snapshots use the current reviewed policy, including explicitly
    listed compatibility paths for documentation published in older releases.
    """
    manifest = Path(__file__).absolute().with_name("public-docs.txt")
    policy_repo = manifest.parent.parent
    if manifest.is_symlink() or manifest.parent.is_symlink() or not manifest.is_file():
        raise ValueError("Documentation manifest must be a regular file")
    data = manifest.read_bytes()
    if verify_index:
        entry = git(
            policy_repo, "ls-files", "--stage", "-z", "--", "scripts/public-docs.txt"
        )
        records = [record for record in entry.split(b"\0") if record]
        if len(records) != 1:
            raise ValueError("Documentation manifest must have one staged version")
        meta, _ = records[0].split(b"\t", 1)
        mode, oid, stage = meta.decode().split()
        if mode not in {"100644", "100755"} or stage != "0":
            raise ValueError(
                "Documentation manifest index is not a regular resolved file"
            )
        if git(policy_repo, "cat-file", "blob", oid) != data:
            raise ValueError("Documentation manifest differs from index")
    return frozenset(
        line for line in data.decode().splitlines() if line and not line.startswith("#")
    )


def private_path(path: str, public_docs: frozenset[str] = frozenset()) -> bool:
    """Permit the reviewed public layout, rejecting unknown documentation paths."""
    parts = PurePosixPath(path).parts
    if path in PUBLIC_ROOT_FILES:
        return False
    if not parts:
        return True
    if any(part.lower() in PRIVATE_PARTS for part in parts):
        return True
    if any(
        part.startswith(".")
        for index, part in enumerate(parts)
        if not (index == 0 and part == ".github")
    ):
        return True
    if parts[0] == "docs":
        return path not in public_docs
    if len(parts) < 2 or parts[0] not in PUBLIC_SOURCE_SUFFIXES:
        return True
    if parts[0] == "custom_components" and (
        len(parts) < 3 or parts[1] != "pv_excess_control"
    ):
        return True
    return PurePosixPath(path).suffix.lower() not in PUBLIC_SOURCE_SUFFIXES[parts[0]]


def check(
    repo: Path, base: str | None = None, head: str = "HEAD"
) -> list[tuple[str, str, str]]:
    public_docs = documentation_policy(verify_index=base is None)
    findings = set()
    seen = set()

    def inspect(label: str, path: str, mode: str, data: bytes):
        key = (path, mode, hashlib.sha256(data).digest())
        if key in seen:
            return
        seen.add(key)
        if private_path(path, public_docs):
            findings.add((label, path, "private path"))
        if mode not in {"100644", "100755"}:
            findings.add((label, path, "symlink or submodule"))
        for reason, pattern in PATTERNS.items():
            if pattern.search(data):
                findings.add((label, path, reason))

    if base is not None:
        # Resolve user-supplied revisions before constructing the range; no shell.
        tip = git(repo, "rev-parse", "--verify", f"{head}^{{commit}}").decode().strip()
        if base == "0" * 40:
            commits = git(repo, "rev-list", tip).decode().splitlines()
        else:
            start = (
                git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
                .decode()
                .strip()
            )
            commits = git(repo, "rev-list", f"{start}..{tip}").decode().splitlines()
        for commit in dict.fromkeys([tip, *commits]):
            for entry in git(repo, "ls-tree", "-rz", commit).split(b"\0"):
                if not entry:
                    continue
                meta, raw_path = entry.split(b"\t", 1)
                mode, kind, oid = meta.decode().split()
                data = git(repo, "cat-file", "blob", oid) if kind == "blob" else b""
                inspect(
                    commit[:12], raw_path.decode("utf-8", "surrogateescape"), mode, data
                )
    else:
        for entry in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
            if not entry:
                continue
            meta, raw_path = entry.split(b"\t", 1)
            mode, oid, stage = meta.decode().split()
            path = raw_path.decode("utf-8", "surrogateescape")
            if stage != "0":
                findings.add(("index", path, "unresolved index conflict"))
            indexed_data = (
                git(repo, "cat-file", "blob", oid) if mode != "160000" else b""
            )
            inspect("index", path, mode, indexed_data)
            target = repo / path
            if not target.exists() and not target.is_symlink():
                continue  # The index was still inspected; an unstaged deletion cannot hide it.
            # Never follow links to private files, including linked parent dirs.
            if target.is_symlink() or repo.resolve() not in target.resolve().parents:
                inspect("working", path, "120000", b"")
            elif target.is_file():
                inspect("working", path, mode, target.read_bytes())
            else:
                inspect("working", path, mode, b"")
    return sorted(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--base", help="Exclusive baseline commit; scans each later commit snapshot"
    )
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    try:
        findings = check(args.repo, args.base, args.head)
    except (OSError, subprocess.CalledProcessError, ValueError):
        print(
            "Public tree check could not validate repository/history or documentation manifest; refusing to pass."
        )
        return 2
    for revision, path, reason in findings:
        # Hash filenames too: an accidentally secret filename must not leak in CI.
        identifier = hashlib.sha256(
            path.encode("utf-8", "surrogateescape")
        ).hexdigest()[:12]
        print(f"{revision}: file {identifier}: {reason}")
    print(f"Public tree check: {len(findings)} finding(s).")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
