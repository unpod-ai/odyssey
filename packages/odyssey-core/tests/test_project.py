from __future__ import annotations

import subprocess

from odyssey.project import ENV_PROJECT, resolve_project


def test_env_var_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_PROJECT, "from-env")
    (tmp_path / "irrelevant.txt").touch()
    assert resolve_project(cwd=tmp_path) == "from-env"


def test_no_git_falls_back_to_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    project_dir = tmp_path / "my-cool-project"
    project_dir.mkdir()
    assert resolve_project(cwd=project_dir) == "my-cool-project"


def test_git_remote_origin_wins_over_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "checkout-dir-name-differs"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/real-repo-name.git"],
        cwd=repo_dir,
        check=True,
    )
    assert resolve_project(cwd=repo_dir) == "real-repo-name"


def test_git_remote_origin_without_dot_git_suffix(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/no-suffix"],
        cwd=repo_dir,
        check=True,
    )
    assert resolve_project(cwd=repo_dir) == "no-suffix"


def test_git_repo_with_no_origin_remote_falls_back_to_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "no-origin-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    assert resolve_project(cwd=repo_dir) == "no-origin-repo"


def test_malformed_git_config_falls_back_to_dirname(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "broken-git-repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    (repo_dir / ".git" / "config").write_text("not = [a valid = git config")
    assert resolve_project(cwd=repo_dir) == "broken-git-repo"


def test_git_dir_found_from_a_subdirectory(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/repo.git"],
        cwd=repo_dir,
        check=True,
    )
    sub = repo_dir / "a" / "b"
    sub.mkdir(parents=True)
    assert resolve_project(cwd=sub) == "repo"
