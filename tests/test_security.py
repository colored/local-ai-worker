from pathlib import Path

import pytest
from pydantic import ValidationError

from local_ai.config import Settings
from local_ai.evidence import Run, retrieve
from local_ai.files import Budget, contained
from local_ai.git import Git
from local_ai.schemas import WorkerError
from local_ai.secrets import Redactor


@pytest.mark.parametrize(
    "secret",
    [
        'password = "supersecretcanary"',
        '{"access_token":"supersecretcanary"}',
        "Cookie: session=supersecretcanary",
        "Authorization: Bearer supersecretcanary",
        "https://alice:supersecretcanary@example.invalid/db",
        "refresh_token=supersecretcanary",
        "session_id=supersecretcanary",
        "-----BEGIN PRIVATE KEY-----\nsupersecretcanary\n-----END PRIVATE KEY-----",
    ],
)
def test_supported_secrets(secret):
    safe, changed = Redactor().screen(secret)
    assert changed and "supersecretcanary" not in safe
    assert safe.count("\n") == secret.count("\n")


def test_authentication_source_is_allowed():
    text = "def authenticate(user):\n    return verify_signature(user)\n"
    assert Redactor().screen(text) == (text, False)


@pytest.mark.parametrize(
    "path", ["../outside", "/etc/passwd", "a/../../outside", "C:/secret", "a\0b"]
)
def test_path_escape_denied(repo, path):
    with pytest.raises(WorkerError):
        contained(repo, path)


def test_symlink_skipped(repo, tmp_path):
    link = repo / "escape"
    try:
        link.symlink_to(tmp_path)
    except OSError:
        pytest.skip("Host does not grant symlink privileges")
    with pytest.raises(WorkerError, match="Symbolic"):
        contained(repo, "escape/secret.txt")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:11434",
        "http://localhost:11434",
        "http://example.com",
        "http://alice:pw@127.0.0.1:11434",
        "http://127.0.0.1:11434/path",
    ],
)
def test_remote_or_credential_endpoint_denied(endpoint):
    with pytest.raises(ValidationError):
        Settings(endpoint=endpoint)


def test_evidence_cursor_and_containment(repo, settings):
    run = Run(Path(settings.state_dir), repo, "project", "off", Redactor())
    eid = run.add("x" * 17000, "src/large.py")
    first = retrieve(Path(settings.state_dir), run.result.run_id, eid, Redactor(), [repo])
    assert len(first.content) == 8192 and first.next_cursor
    second = retrieve(
        Path(settings.state_dir), run.result.run_id, eid, Redactor(), [repo], first.next_cursor
    )
    assert len(second.content) == 8192
    with pytest.raises(WorkerError):
        retrieve(Path(settings.state_dir), "../escape", eid, Redactor(), [repo])


def test_git_scope_includes_denied(repo):
    with (repo / ".git" / "config").open("a") as stream:
        stream.write("\n[include]\npath = /outside/config\n")
    with pytest.raises(WorkerError, match="includes"):
        Git(repo, Budget(5, 10000))


def test_structured_screen_preserves_schema_quotes():
    raw = {"text": 'Investigate password="supersecretcanary"', "statistics": {"events": 4}}
    safe = Redactor().object(raw)
    assert "supersecretcanary" not in safe["text"]
    assert safe["statistics"]["events"] == 4
