import json

from local_ai.schemas import LogRequest
from local_ai.service import analyze, evidence


async def test_long_failures_with_different_endings_not_merged(repo, settings):
    common = "shared context " * 400
    (repo / "large.jsonl").write_text(
        "\n".join(
            json.dumps({"level": "ERROR", "message": common + suffix})
            for suffix in ("disk failure", "network failure")
        ),
        encoding="utf-8",
    )
    result = await analyze(
        "logs", LogRequest(paths=["large.jsonl"], profile="off"), settings=settings
    )
    assert result.statistics["groups"] == 2


async def test_indented_jsonl_records_are_separate(repo, settings):
    (repo / "space.jsonl").write_text(
        '  {"level":"ERROR","message":"failed"}\n  {"level":"ERROR","message":"failed"}\n',
        encoding="utf-8",
    )
    result = await analyze(
        "logs", LogRequest(paths=["space.jsonl"], profile="off"), settings=settings
    )
    assert result.statistics["events_parsed"] == 2


async def test_timezone_aware_timeline_order(repo, settings):
    (repo / "time.log").write_text(
        "2026-10-01T09:00:00Z INFO request_id=a completed\n2026-10-01T10:00:00+02:00 INFO request_id=a started\n",
        encoding="utf-8",
    )
    result = await analyze("logs", LogRequest(paths=["time.log"], profile="off"), settings=settings)
    link = next(f for f in result.facts if "links 2 events" in f.text)
    first = evidence(result.run_id, link.evidence_ids[0], settings=settings)
    assert "started" in first.content
