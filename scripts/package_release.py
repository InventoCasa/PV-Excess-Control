#!/usr/bin/env python3
"""Build a reproducible component-only archive for manual installation.

Package sources must match the index; untracked files are excluded.
HACS continues to use the repository source layout. This archive deliberately
retains custom_components/pv_excess_control rather than changing HACS settings.
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

from check_public_tree import check, git, private_path


def build(repo: Path, output: Path) -> None:
    if check(repo):
        raise ValueError("Public tree check failed; refusing to package")
    if git(
        repo,
        "diff",
        "--name-only",
        "--",
        "LICENSE",
        "custom_components/pv_excess_control/",
    ):
        raise ValueError(
            "Package sources differ from index; stage or restore them first"
        )
    component = repo / "custom_components" / "pv_excess_control"
    manifest = json.loads((component / "manifest.json").read_text())
    if manifest.get("domain") != "pv_excess_control" or not manifest.get("version"):
        raise ValueError("Invalid integration manifest")
    files = [repo / "LICENSE"]
    for raw_path in git(
        repo, "ls-files", "-z", "--", "custom_components/pv_excess_control/"
    ).split(b"\0"):
        if not raw_path:
            continue
        path = repo / raw_path.decode("utf-8", "surrogateescape")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files.append(path)
    for path in files:
        relative = path.relative_to(repo).as_posix()
        if (
            path.is_symlink()
            or repo.resolve() not in path.resolve().parents
            or private_path(relative)
        ):
            raise ValueError("Unsafe package member")
        if path != repo / "LICENSE" and path.suffix not in {
            ".py",
            ".json",
            ".yaml",
            ".yml",
            ".js",
            ".css",
            ".svg",
            ".png",
        }:
            raise ValueError("Unexpected package member type")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(
                path.relative_to(repo).as_posix(), date_time=(2020, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    print(f"Built {manifest['version']}: {len(files)} files")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.repo.resolve(), args.output)
    except (OSError, ValueError, zipfile.BadZipFile):
        print("Release package validation failed.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
