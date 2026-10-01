import pytest

from local_ai.config import project_root
from local_ai.schemas import ProjectRequest, WorkerError
from local_ai.service import analyze, evidence


async def test_project_and_evidence_survive_restart(repo, settings):
    result = await analyze(
        "project", ProjectRequest(focus="retry", profile="off"), settings=settings, mcp=True
    )
    assert result.status != "failed"
    assert result.statistics["files_considered"] == 4
    assert result.model.used is False
    assert any("Python" in f.text for f in result.facts)
    item = next(e for e in result.evidence if e.path == "src/retry.py")
    (repo / "src/retry.py").write_text("changed later", encoding="utf-8")
    page = evidence(result.run_id, item.id, settings=settings, mcp=True)
    assert "return 1" in page.content
    assert page.lines == [1, 2]


async def test_screening_on_disk_and_response(repo, settings):
    canary = "fixture-super-secret-auth-value"
    (repo / "src" / "auth.py").write_text(
        f'password = "{canary}"\ndef authenticate(): pass\n', encoding="utf-8"
    )
    result = await analyze(
        "project", ProjectRequest(focus="auth", profile="off"), settings=settings
    )
    assert any(e.path == "src/auth.py" for e in result.evidence)
    for path in (settings_state(settings) / "runs" / result.run_id).rglob("*.json"):
        assert canary not in path.read_text("utf-8")
    assert canary not in result.model_dump_json()


def settings_state(settings):
    from pathlib import Path

    return Path(settings.state_dir)


def test_root_override_policy(repo, settings, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert project_root(settings) == repo
    with pytest.raises(WorkerError, match="permitted"):
        project_root(settings, str(other), mcp=True)
    settings.additional_roots = [str(other)]
    assert project_root(settings, str(other), mcp=True) == other


async def test_other_repository_evidence_denied(repo, settings, tmp_path, monkeypatch):
    result = await analyze("project", ProjectRequest(profile="off"), settings=settings)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    with pytest.raises(WorkerError, match="another project"):
        evidence(result.run_id, result.evidence[0].id, settings=settings, mcp=True)


async def test_unknown_stack_no_project_configuration(repo, settings):
    (repo / "thing.xyz").write_text("an unrelated language", encoding="utf-8")
    result = await analyze("project", ProjectRequest(profile="off"), settings=settings)
    assert result.statistics["files_considered"] == 5


async def test_configuration_gating_keeps_deterministic_results(repo, settings):
    result = await analyze("project", ProjectRequest(), settings=settings)
    assert result.facts and not result.model.used
    assert any("cloud disabled" in item for item in result.limitations)
