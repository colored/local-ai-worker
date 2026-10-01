import json

import httpx
import pytest

from local_ai.evidence import Run
from local_ai.ollama import assist, inference_lock
from local_ai.schemas import ProjectRequest
from local_ai.secrets import Redactor
from local_ai.service import analyze


def mock_ollama(handler=None):
    def route(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})
        if handler:
            return handler(request)
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {
                    "content": json.dumps(
                        {
                            "hypotheses": [
                                {
                                    "id": "model-id",
                                    "text": "Inspect retry handling.",
                                    "evidence_ids": ["e1"],
                                }
                            ],
                            "clusters": [],
                        }
                    )
                },
            },
        )

    return httpx.MockTransport(route)


async def test_valid_reply_and_bounded_prompt(repo, settings):
    settings.local_only_confirmed = True
    result = await analyze("project", ProjectRequest(), settings=settings, transport=mock_ollama())
    assert result.model.used
    assert result.hypotheses[0].id == "h1"
    assert result.statistics["context_tokens_selected"] > 0


async def test_malformed_and_invented_evidence_retry_then_fallback(repo, settings):
    settings.local_only_confirmed = True
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "hypotheses": [
                                {"id": "h", "text": "invented", "evidence_ids": ["e99999"]}
                            ]
                        }
                    )
                }
            },
        )

    result = await analyze(
        "project", ProjectRequest(), settings=settings, transport=mock_ollama(handler)
    )
    assert len(calls) == 2
    assert result.facts and not result.hypotheses and not result.model.used


async def test_unavailable_model_returns_facts(repo, settings):
    settings.local_only_confirmed = True

    def unavailable(request):
        raise httpx.ConnectError("private-token=do-not-echo")

    result = await analyze(
        "project", ProjectRequest(), settings=settings, transport=httpx.MockTransport(unavailable)
    )
    assert result.facts and not result.model.used
    assert "do-not-echo" not in result.model_dump_json()


async def test_secret_screening_before_prompt_and_after_reply(repo, settings):
    settings.local_only_confirmed = True
    (repo / "source.txt").write_text('api_key="prompt-secret-canary"', encoding="utf-8")

    def handler(request):
        assert "prompt-secret-canary" not in request.content.decode()
        response = {
            "hypotheses": [
                {
                    "id": "h",
                    "text": 'password="output-secret-canary" may be exposed',
                    "evidence_ids": ["e1"],
                }
            ]
        }
        return httpx.Response(200, json={"message": {"content": json.dumps(response)}})

    result = await analyze(
        "project", ProjectRequest(focus="source"), settings=settings, transport=mock_ollama(handler)
    )
    assert result.model.used
    assert "output-secret-canary" not in result.model_dump_json()


async def test_busy_returns_without_http(repo, settings):
    from pathlib import Path

    settings.local_only_confirmed = True
    with inference_lock(Path(settings.state_dir)) as acquired:
        assert acquired
        result = await analyze(
            "project", ProjectRequest(), settings=settings, transport=mock_ollama()
        )
    assert not result.model.used and any("busy" in item for item in result.limitations)


async def test_model_timeout(repo, settings):
    import asyncio
    from pathlib import Path

    settings.local_only_confirmed = True
    run = Run(Path(settings.state_dir), repo, "project", "fast", Redactor())
    run.add("evidence", "source.py")

    async def wait(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"models": []})

    await assist(run, settings, None, transport=httpx.MockTransport(wait), seconds=0.02)
    assert any("deadline" in item for item in run.result.limitations)


@pytest.mark.parametrize("marker", ["stale", "corrupt", "absent"])
async def test_shared_same_model_never_unloaded(repo, settings, marker):
    from pathlib import Path

    settings.local_only_confirmed = True
    settings.keep_alive = "0"
    state = Path(settings.state_dir)
    state.mkdir()
    if marker != "absent":
        (state / "resident.json").write_text(
            '{"model": "gemma3:12b"}' if marker == "stale" else "not json"
        )

    def handler(request):
        assert request.url.path != "/api/generate"
        if request.url.path in {"/api/ps", "/api/tags"}:
            return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})
        assert "keep_alive" not in json.loads(request.content)
        return httpx.Response(200, json={"message": {"content": '{"hypotheses": []}'}})

    result = await analyze(
        "project", ProjectRequest(), settings=settings, transport=httpx.MockTransport(handler)
    )
    assert result.model.used


async def test_opt_in_switch_waits_for_expiry_without_unload(repo, settings):
    settings.local_only_confirmed = True
    settings.managed_model_switching = True
    polls = 0

    def handler(request):
        nonlocal polls
        assert request.url.path != "/api/generate"
        if request.url.path == "/api/ps":
            polls += 1
            return httpx.Response(
                200, json={"models": [{"name": "gemma3:27b"}] if polls == 1 else []}
            )
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma3:12b"}]})
        assert polls == 2
        return httpx.Response(200, json={"message": {"content": '{"hypotheses": []}'}})

    result = await analyze(
        "project", ProjectRequest(), settings=settings, transport=httpx.MockTransport(handler)
    )
    assert result.model.used


async def test_switch_wait_is_bounded(repo, settings):
    from pathlib import Path

    settings.local_only_confirmed = True
    settings.managed_model_switching = True
    run = Run(Path(settings.state_dir), repo, "project", "fast", Redactor())
    run.add("evidence", "source.py")

    def handler(request):
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [{"name": "gemma3:27b"}]})

    await assist(run, settings, None, transport=httpx.MockTransport(handler), seconds=0.03)
    assert not run.result.model.used
    assert any("deadline" in item for item in run.result.limitations)
    run.close()
