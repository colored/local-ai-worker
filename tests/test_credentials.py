import json
from pathlib import Path

import httpx
import pytest
from conftest import git

from local_ai.files import Budget, read_text
from local_ai.git import Git
from local_ai.schemas import DiffRequest, LogRequest, ProjectRequest
from local_ai.secrets import secret_container
from local_ai.service import analyze

DENIED = [
    ".env",
    ".env.production",
    "nested/.env.local",
    ".aws/config",
    ".ssh/config",
    ".gnupg/keyring",
    ".kube/config",
    ".docker/config.json",
    ".config/gcloud/configurations/config_default",
    ".config/gh/hosts.yml",
    ".password-store/example.gpg",
    ".pgpass",
    "credentials.json",
    "private.pem",
    "vault/key.jks",
    "custom/opaque.txt",
]
CANARY = "opaque-canary-credential-without-any-recognizable-secret-syntax"


@pytest.mark.parametrize("path", DENIED)
def test_denied_before_open(repo, path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Denied file was opened")

    monkeypatch.setattr(Path, "open", forbidden)
    assert read_text(repo, path, Budget(5, 10000, ["custom/*"]), 10000) is None


@pytest.mark.parametrize(
    "path",
    [
        "src/auth.py",
        "src/authentication.ts",
        "tests/test_login.py",
        "src/token.ts",
        "docs/authentication.md",
    ],
)
def test_authentication_source_allowed(path):
    assert not secret_container(path)


@pytest.mark.parametrize(
    "task,analysis_request",
    [
        ("project", ProjectRequest(focus="opaque")),
        ("logs", LogRequest(paths=["."])),
        *(("diff", DiffRequest(mode=mode)) for mode in ["staged", "unstaged", "combined"]),
        ("diff", DiffRequest(mode="direct", base_ref="HEAD~1")),
        ("diff", DiffRequest(mode="merge_base", base_ref="HEAD~1")),
    ],
)
async def test_canaries_never_read_or_reach_any_output(
    repo, settings, monkeypatch, caplog, task, analysis_request
):
    settings.local_only_confirmed = True
    settings.sensitive_path_patterns = ["custom/*"]
    for name in DENIED:
        path = repo / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(CANARY, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "credential fixtures")
    for name in DENIED:
        (repo / name).write_text(CANARY + " changed", encoding="utf-8")
    git(repo, "add", ".")
    for name in DENIED:
        (repo / name).write_text(CANARY + " unstaged", encoding="utf-8")
    (repo / "safe.log").write_text("ERROR useful event\n", encoding="utf-8")
    original_open, original_run = Path.open, Git.run

    def checked_open(path, *args, **kwargs):
        if path.is_relative_to(repo) and secret_container(
            path.relative_to(repo).as_posix(), settings.sensitive_path_patterns
        ):
            pytest.fail("Denied source was opened")
        return original_open(path, *args, **kwargs)

    def checked_git(self, *args, **kwargs):
        if args[0] == "diff":
            paths = args[args.index("--") + 1 :]
            assert paths  # An unrestricted Git diff may read denied content.
            assert all(not self.budget.denied(p) for p in paths)
        if args[0] == "show":
            assert not self.budget.denied(args[1].split(":", 1)[1])
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    monkeypatch.setattr(Git, "run", checked_git)
    prompts = []

    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})
        prompts.append(req.content.decode())
        return httpx.Response(200, json={"message": {"content": json.dumps({"hypotheses": []})}})

    result = await analyze(
        task, analysis_request, settings=settings, transport=httpx.MockTransport(handler)
    )
    assert prompts
    assert CANARY not in result.model_dump_json() + caplog.text + "".join(prompts)
    for path in Path(settings.state_dir).rglob("*.json"):
        assert CANARY not in path.read_text("utf-8")
