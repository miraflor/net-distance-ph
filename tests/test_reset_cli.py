"""Isolated tests for the native reset command; NEVER touch a real workspace."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

import pytest
from net_distance_ph import reset


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, text=True, capture_output=True)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout.strip()


def args(**kwargs):
    values = dict(workspace=None, all=True, execute=False, run=False,
                  purge_patch_artifacts=False, workers=2, heartbeat_seconds=30.0)
    values.update(kwargs)
    return argparse.Namespace(**values)


@pytest.fixture
def fixture_repo(tmp_path):
    # Never access the user's real Documents, Downloads or remote.
    home = tmp_path / "home"
    repo = tmp_path / "source" / "net-distance-ph"
    bare = tmp_path / "remote.git"
    ws = home / "Documents" / "net-distance-ph"
    repo.mkdir(parents=True)
    (repo / "net_distance_ph").mkdir()
    (repo / "net_distance_ph" / "cli.py").write_text("# test CLI\n")
    (repo / ".gitignore").write_text("*.tmp\n")
    git(tmp_path, "init", "--bare", str(bare))
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    git(repo, "checkout", "-b", "feat/reset")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    git(repo, "push", "-u", "origin", "feat/reset")
    (repo / "local.tmp").write_text("ignored scratch")
    ws.mkdir(parents=True)
    (ws / "checkpoint.npy").write_bytes(b"old result")
    return repo, home, ws


def test_preview_clears_nothing(fixture_repo):
    repo, home, ws = fixture_repo
    assert reset.run(args(), repo_override=repo, home_override=home) == 0
    assert (repo / "local.tmp").exists()
    assert (ws / "checkpoint.npy").exists()


def test_execute_removes_workspace_and_generated_repo_artifacts(fixture_repo, monkeypatch):
    repo, home, ws = fixture_repo
    monkeypatch.setattr("builtins.input", lambda prompt: reset.CONFIRM)
    assert reset.run(args(execute=True), repo_override=repo, home_override=home) == 0
    assert not ws.exists()
    assert not (repo / "local.tmp").exists()
    assert (repo / "net_distance_ph" / "cli.py").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_workspace_only_preserves_repo_artifacts(fixture_repo, monkeypatch):
    repo, home, ws = fixture_repo
    monkeypatch.setattr("builtins.input", lambda prompt: reset.CONFIRM)
    assert reset.run(args(all=False, execute=True), repo_override=repo, home_override=home) == 0
    assert not ws.exists()
    assert (repo / "local.tmp").exists()


def test_purge_is_narrow_and_opt_in(fixture_repo, monkeypatch):
    repo, home, ws = fixture_repo
    d = home / "Downloads"
    d.mkdir()
    historical = d / "net_distance_ph_fresh_v45_1.zip"
    historical.write_bytes(b"old helper")
    unrelated = d / "unrelated_archive.zip"
    unrelated.write_bytes(b"keep")
    backup = repo.parent / "net-distance-ph-timing-backup"
    backup.mkdir()
    (backup / "old.py").write_text("old")
    monkeypatch.setattr("builtins.input", lambda prompt: reset.CONFIRM)
    assert reset.run(args(execute=True, purge_patch_artifacts=True),
                     repo_override=repo, home_override=home) == 0
    assert not historical.exists()
    assert not backup.exists()
    assert unrelated.exists()


def test_requires_clean_branch_before_repo_reset(fixture_repo):
    repo, home, ws = fixture_repo
    (repo / "untracked.txt").write_text("important")
    with pytest.raises(RuntimeError, match="Uncommitted|untracked"):
        reset.run(args(execute=False), repo_override=repo, home_override=home)
    assert ws.exists()


def test_rejects_unsafe_workspace_and_incompatible_flags(fixture_repo):
    repo, home, ws = fixture_repo
    with pytest.raises(ValueError, match="Workspace"):
        reset.run(args(workspace=repo), repo_override=repo, home_override=home)
    with pytest.raises(ValueError, match="requires --all"):
        reset.run(args(all=False, run=False, purge_patch_artifacts=True), repo_override=repo, home_override=home)
    with pytest.raises(ValueError, match="requires --all --execute"):
        reset.run(args(run=True), repo_override=repo, home_override=home)


def test_declined_confirmation_removes_nothing(fixture_repo, monkeypatch):
    repo, home, ws = fixture_repo
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert reset.run(args(execute=True), repo_override=repo, home_override=home) == 1
    assert ws.exists()
    assert (repo / "local.tmp").exists()


def test_unpushed_commit_refused(fixture_repo):
    repo, home, ws = fixture_repo
    (repo / "net_distance_ph" / "cli.py").write_text("# modified CLI\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "local change")
    with pytest.raises(RuntimeError, match="is not equal to"):
        reset.run(args(execute=False), repo_override=repo, home_override=home)
    assert ws.exists()
