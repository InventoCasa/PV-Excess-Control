"""Regression coverage for the public repository boundary (synthetic data only)."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_public_tree.py"
PACKAGER = ROOT / "scripts" / "package_release.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Synthetic Test")
    git(tmp_path, "config", "user.email", "test@example.org")
    (tmp_path / "README.md").write_text("Public documentation\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Initial public tree")
    return tmp_path


def run_guard(repo, *args):
    assert GUARD.exists(), "Public repository checker must be implemented"
    return subprocess.run(
        [sys.executable, str(GUARD), "--repo", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "LOCAL_NOTES.md",
        "docs/LOCAL_NOTES.md",
        ".env",
        ".env.production",
        ".settings/settings.json",
        ".context/notes.md",
        ".local/settings.json",
        "secrets/note.md",
        "nested/.context/notes.md",
        "nested/SECRETS/note.md",
        "certificate.key",
        "nested/certificate.pem",
        "nested/CERTIFICATE.PEM",
        "nested/.env.test",
        ".env.example",
        "docs/deployments/live.md",
        "docs/local-notes.md",
        "tests/local-notes.md",
        "scripts/.settings/config.json",
        "custom_components/pv_excess_control/LOCAL_NOTES.md",
    ],
)
def test_reject_private_paths(repo, path):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic private instructions")
    git(repo, "add", "--force", path)
    result = run_guard(repo)
    assert result.returncode == 1
    assert "private path" in result.stdout


@pytest.mark.parametrize(
    "content",
    [
        "url = 'http://" + ".".join(["192", "168", "2", "9"]) + ":8123'",  # noqa: FLY002
        "path = '/home/" + "developer/private-instance'",
        "token = '" + "eyJ" + "h" * 20 + "." + "b" * 35 + "." + "c" * 32 + "'",
        "HA_TOKEN = '" + "synthetic" * 10 + "'",
        "-----BEGIN " + "RSA PRIVATE KEY-----",
    ],
)
def test_reject_private_content_without_echoing_value(repo, content):
    (repo / "settings.py").write_text(content)
    git(repo, "add", "settings.py")
    result = run_guard(repo)
    assert result.returncode == 1
    assert content not in result.stdout + result.stderr


def test_allow_environment_lookup_and_sensor_fixtures(repo):
    (repo / "tests").mkdir()
    (repo / "tests/test_config.py").write_text(
        'HA_TOKEN = os.environ["HA_TOKEN"]\nentity = "sensor.test_power"\nurl = "http://localhost:8123"\n'
    )
    git(repo, "add", "tests/test_config.py")
    assert run_guard(repo).returncode == 0


def test_untracked_private_local_notes_are_outside_public_tree(repo):
    (repo / "LOCAL_NOTES.md").write_text("Local only")
    assert run_guard(repo).returncode == 0


@pytest.mark.parametrize("initial_push", [False, True])
def test_deleted_secret_is_still_detected_in_new_history(repo, initial_push):
    base = "0" * 40 if initial_push else git(repo, "rev-parse", "HEAD")
    (repo / "credentials.py").write_text("HA_TOKEN = '" + "synthetic" * 10 + "'")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Synthetic accidental addition")
    git(repo, "rm", "-q", "credentials.py")
    git(repo, "commit", "-qm", "Remove it again")
    result = run_guard(repo, "--base", base, "--head", "HEAD")
    assert result.returncode == 1
    assert "credential literal" in result.stdout


def test_symlink_cannot_read_outside_repository(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "value"
    outside.write_text("Must not be read")
    (repo / "leak.txt").symlink_to(outside)
    git(repo, "add", "leak.txt")
    result = run_guard(repo)
    assert result.returncode == 1
    assert "symlink" in result.stdout


def test_release_zip_contains_only_integration_and_license(tmp_path):
    assert PACKAGER.exists(), "Release packager must be implemented"
    target = tmp_path / "release.zip"
    result = subprocess.run(
        [sys.executable, str(PACKAGER), "--repo", str(ROOT), "--output", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert "custom_components/pv_excess_control/manifest.json" in names
        assert "LICENSE" in names
        assert all(
            name == "LICENSE" or name.startswith("custom_components/pv_excess_control/")
            for name in names
        )
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)


def test_package_excludes_untracked_component_files(repo, tmp_path):
    component = repo / "custom_components" / "pv_excess_control"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(
        '{"domain":"pv_excess_control","version":"0.1.0"}'
    )
    (component / "__init__.py").write_text('"""Synthetic component."""')
    (repo / "LICENSE").write_text("Synthetic license")
    git(repo, "add", ".")
    (component / "local_settings.py").write_text("private local data")
    target = tmp_path / "release.zip"
    result = subprocess.run(
        [sys.executable, str(PACKAGER), "--repo", str(repo), "--output", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    with zipfile.ZipFile(target) as archive:
        assert not any(
            name.endswith("local_settings.py") for name in archive.namelist()
        )


def test_initial_branch_push_accepts_clean_history(repo):
    assert run_guard(repo, "--base", "0" * 40, "--head", "HEAD").returncode == 0


@pytest.mark.parametrize("working_change", ["sanitize", "delete"])
def test_staged_secret_survives_unstaged_working_change(repo, working_change):
    target = repo / "settings.py"
    target.write_text("HA_TOKEN = '" + "synthetic" * 10 + "'")
    git(repo, "add", "settings.py")
    if working_change == "sanitize":
        target.write_text("HA_TOKEN = os.environ['HA_TOKEN']")
    else:
        target.unlink()
    result = run_guard(repo)
    assert result.returncode == 1
    assert "index" in result.stdout
    assert "credential literal" in result.stdout


def test_unresolved_index_conflict_fails_even_with_clean_working_file(repo):
    git(repo, "checkout", "-qb", "other")
    (repo / "README.md").write_text("Other clean documentation\n")
    git(repo, "commit", "-qam", "Other change")
    git(repo, "checkout", "-qb", "candidate", "HEAD~1")
    (repo / "README.md").write_text("Candidate clean documentation\n")
    git(repo, "commit", "-qam", "Candidate change")
    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "other"], check=False, capture_output=True
    )
    assert merge.returncode == 1
    (repo / "README.md").write_text("Resolution not yet staged\n")
    result = run_guard(repo)
    assert result.returncode == 1
    assert "unresolved index conflict" in result.stdout


def test_package_rejects_unstaged_component_changes(repo, tmp_path):
    component = repo / "custom_components" / "pv_excess_control"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(
        '{"domain":"pv_excess_control","version":"0.1.0"}'
    )
    (component / "__init__.py").write_text('"""Original component."""')
    (repo / "LICENSE").write_text("Synthetic license")
    git(repo, "add", ".")
    (component / "__init__.py").write_text('"""Unstaged component."""')
    result = subprocess.run(
        [
            sys.executable,
            str(PACKAGER),
            "--repo",
            str(repo),
            "--output",
            str(tmp_path / "release.zip"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1


def test_allow_public_documentation_manifest_entry(repo):
    (repo / "docs").mkdir()
    (repo / "docs/installation.md").write_text("Public installation instructions\n")
    git(repo, "add", "docs/installation.md")
    assert run_guard(repo).returncode == 0


@pytest.mark.parametrize("manifest_change", ["unstaged_allowlist", "delete", "symlink"])
def test_documentation_manifest_must_match_index(
    repo, tmp_path_factory, manifest_change
):
    scripts = repo / "scripts"
    scripts.mkdir()
    checker = scripts / "check_public_tree.py"
    checker.write_text(GUARD.read_text())
    manifest = scripts / "public-docs.txt"
    manifest.write_text("docs/installation.md\n")
    git(repo, "add", "scripts")
    (repo / "docs").mkdir()
    (repo / "docs/internal-runbook.md").write_text(
        "Synthetic private operating notes\n"
    )
    git(repo, "add", "docs/internal-runbook.md")
    if manifest_change == "unstaged_allowlist":
        manifest.write_text("docs/installation.md\ndocs/internal-runbook.md\n")
    else:
        manifest.unlink()
        if manifest_change == "symlink":
            outside = tmp_path_factory.mktemp("policy") / "public-docs.txt"
            outside.write_text("docs/internal-runbook.md\n")
            manifest.symlink_to(outside)
    result = subprocess.run(
        [sys.executable, str(checker), "--repo", str(repo)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "manifest" in result.stdout
    assert "Traceback" not in result.stderr
