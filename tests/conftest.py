import subprocess
from pathlib import Path

import pytest

from local_ai.config import Settings


def git(root: Path, *args):
    return (
        subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "retry.py").write_text("def retry():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_retry.py").write_text(
        "from src.retry import retry\ndef test_retry():\n    assert retry() == 1\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Fixture\nA background retry component.\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname="fixture"\n', encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


@pytest.fixture
def settings(tmp_path):
    return Settings(state_dir=str(tmp_path / "state"))
