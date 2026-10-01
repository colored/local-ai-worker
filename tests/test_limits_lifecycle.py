import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from conftest import git
from typer.testing import CliRunner

from local_ai.cli import app
from local_ai.context import assemble
from local_ai.evidence import Run
from local_ai.mcp_server import create_server
from local_ai.ollama import assist
from local_ai.schemas import DiffRequest, LogRequest, ProjectRequest
from local_ai.secrets import Redactor
from local_ai.service import analyze


async def test_git_index_refs_remain_unchanged(repo, settings):
    (repo / "src" / "retry.py").write_text("x = 2", encoding="utf-8")

    def fingerprint():
        return {
            p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (repo / ".git").rglob("*")
            if p.is_file()
        }

    before = fingerprint()
    await analyze("diff", DiffRequest(profile="off"), settings=settings)
    await analyze("project", ProjectRequest(profile="off"), settings=settings)
    assert fingerprint() == before


async def test_worktree_and_component_root(repo, settings, tmp_path, monkeypatch):
    root = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(root))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root / "src"))
    result = await analyze("project", ProjectRequest(profile="off"), settings=settings)
    assert result.facts and result.status != "failed"


def test_context_omission_is_reported(repo, settings):
    run = Run(Path(settings.state_dir), repo, "project", "fast", Redactor())
    run.add("short useful evidence", "README.md")
    run.add("word " * 40000, "large.py")
    messages, ids, metrics = assemble(run, settings.profiles["fast"], None)
    assert ids == {"e1"}
    assert metrics["context_evidence_omitted"] == 1
    assert any("omitted" in item for item in run.result.limitations)


async def test_switch_unloads_before_chat(repo, settings):
    settings.local_only_confirmed = True
    resident = ["gemma3:27b"]
    state = Path(settings.state_dir)
    state.mkdir()
    (state / "resident.json").write_text(json.dumps({"model": resident[0]}), encoding="utf-8")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": n} for n in resident]})
        if request.url.path == "/api/generate":
            assert json.loads(request.content)["keep_alive"] == 0
            resident.clear()
            return httpx.Response(200, json={"done": True})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})
        assert not resident
        return httpx.Response(
            200, json={"message": {"content": '{"hypotheses": [], "clusters": []}'}}
        )

    result = await analyze(
        "project", ProjectRequest(), settings=settings, transport=httpx.MockTransport(handler)
    )
    assert result.model.used
    assert calls.index("/api/generate") < calls.index("/api/chat")


async def test_other_application_resident_not_unloaded(repo, settings):
    settings.local_only_confirmed = True

    def handler(request):
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [{"name": "foreign-model:30b"}]})

    result = await analyze(
        "project", ProjectRequest(), settings=settings, transport=httpx.MockTransport(handler)
    )
    assert result.facts and not result.model.used


async def test_cancel_releases_model_lock(repo, settings):
    settings.local_only_confirmed = True
    started = asyncio.Event()
    run = Run(Path(settings.state_dir), repo, "project", "fast", Redactor())

    async def handler(request):
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(assist(run, settings, None, transport=httpx.MockTransport(handler)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    from local_ai.ollama import inference_lock

    with inference_lock(Path(settings.state_dir)) as acquired:
        assert acquired


async def test_mcp_validation_does_not_echo_secret():
    server = create_server()
    result = await server.call_tool("inspect_project", {"profile": "password=validation-canary"})
    assert result.isError
    assert "validation-canary" not in result.model_dump_json()


def test_cli_validation_does_not_echo_secret():
    result = CliRunner().invoke(app, ["inspect", "--profile", "password=validation-canary"])
    assert result.exit_code == 2
    assert "validation-canary" not in result.output


async def test_semantic_cluster_counts_are_computed(repo, settings):
    settings.local_only_confirmed = True
    (repo / "run.jsonl").write_text(
        "\n".join(
            json.dumps({"level": "ERROR", "message": m})
            for m in ["failed A", "failed A", "failed B"]
        ),
        encoding="utf-8",
    )

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": '{"hypotheses": [], "clusters": [{"name": "Failures", "group_ids": ["g1", "g2"]}]}'
                }
            },
        )

    result = await analyze(
        "logs",
        LogRequest(paths=["run.jsonl"]),
        settings=settings,
        transport=httpx.MockTransport(handler),
    )
    assert result.model.used
    assert any("contains 3 events" in f.text for f in result.facts)
    assert any("Failures" in h.text for h in result.hypotheses)


async def test_read_budget_returns_partial_log_counts(repo, settings):
    settings.max_log_bytes = 1024
    (repo / "run.log").write_text("ERROR a\n" * 1000, encoding="utf-8")
    result = await analyze("logs", LogRequest(paths=["run.log"], profile="off"), settings=settings)
    assert result.status == "partial"
    assert result.facts
    assert 0 < result.statistics["events_parsed"] < 1000
