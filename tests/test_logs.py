import json
from pathlib import Path

from local_ai.schemas import LogRequest
from local_ai.service import analyze, evidence


def log_file(repo, name, content):
    base = repo / "artifacts" / "run-123"
    base.mkdir(parents=True, exist_ok=True)
    (base / name).write_text(content, encoding="utf-8")


async def test_plain_grouping_correlation_and_query(repo, settings):
    log_file(
        repo,
        "run.log",
        "\n".join(
            [
                "2026-10-01T10:00:00Z INFO request_id=req-1 started",
                "2026-10-01T10:00:01Z ERROR request_id=req-1 failed retry",
                "  at retry.py:45",
                "2026-10-01T10:00:02Z ERROR request_id=req-2 failed retry",
                "  at retry.py:46",
            ]
        ),
    )
    result = await analyze(
        "logs",
        LogRequest(
            paths=["artifacts/run-123"], query="likely cause of failed calls", profile="off"
        ),
        settings=settings,
    )
    assert result.statistics["events_parsed"] == 3
    assert result.statistics["events_included"] == 3
    assert result.statistics["groups"] == 2
    assert result.statistics["level_error"] == 2
    assert any("links 2 events" in f.text for f in result.facts)
    failure = next(f for f in result.facts if "occurred 2 times" in f.text)
    page = evidence(result.run_id, failure.evidence_ids[0], settings=settings)
    assert "at retry.py" in page.content
    assert page.lines == [2, 3]


async def test_jsonl_fields_secrets_and_filters(repo, settings):
    entries = [
        {
            "timestamp": "2026-10-01T10:00:00Z",
            "level": "error",
            "message": "failed",
            "request_id": "a",
            "password": "canary-password",
        },
        {
            "timestamp": "2026-10-01T10:01:00Z",
            "level": "error",
            "message": "failed",
            "request_id": "b",
        },
        {"level": "error", "message": "unknown time"},
    ]
    log_file(repo, "run.jsonl", "\n".join(json.dumps(e) for e in entries))
    result = await analyze(
        "logs",
        LogRequest(paths=["artifacts/run-123"], since="2026-10-01T10:00:30Z", profile="off"),
        settings=settings,
    )
    assert result.statistics["events_included"] == 1
    assert result.statistics["events_unknown_time"] == 1
    assert result.statistics["events_filtered"] == 2
    assert "canary-password" not in result.model_dump_json()
    all_text = "".join(
        p.read_text("utf-8") for p in (Path(settings.state_dir) / "runs").rglob("*.json")
    )
    assert "canary-password" not in all_text


async def test_json_array_and_malformed_document(repo, settings):
    log_file(
        repo, "valid.json", json.dumps([{"message": "failed", "level": "ERROR"}] * 3, indent=2)
    )
    log_file(repo, "bad.json", "not JSON")
    result = await analyze(
        "logs", LogRequest(paths=["artifacts/run-123"], profile="off"), settings=settings
    )
    assert result.statistics["events_parsed"] == 3
    assert result.statistics["groups"] == 1
    assert any("malformed_json" in item for item in result.limitations)


async def test_tracebacks_remain_whole(repo, settings):
    log_file(
        repo,
        "stack.log",
        "ERROR exception\nTraceback (most recent call last):\n  File x.py, line 12\nValueError: bad\nINFO complete\n",
    )
    result = await analyze(
        "logs", LogRequest(paths=["artifacts/run-123"], profile="off"), settings=settings
    )
    assert result.statistics["events_parsed"] == 2
    page = evidence(
        result.run_id, next(e.id for e in result.evidence if e.source == "log"), settings=settings
    )
    assert "ValueError" in page.content


async def test_oversized_line_does_not_create_fake_events(repo, settings):
    settings.max_event_bytes = 1024
    log_file(repo, "large.log", "x" * 3000 + "\nERROR real failure\n")
    result = await analyze(
        "logs", LogRequest(paths=["artifacts/run-123"], profile="off"), settings=settings
    )
    assert result.statistics["events_parsed"] == 1
    assert any("oversized_event" in item for item in result.limitations)


async def test_time_filter_requires_timezone(repo, settings):
    log_file(repo, "a.log", "INFO hi")
    result = await analyze(
        "logs",
        LogRequest(paths=["artifacts/run-123"], since="2026-10-01T10:00:00", profile="off"),
        settings=settings,
    )
    assert result.status == "failed"
