from conftest import git

from local_ai.schemas import DiffRequest
from local_ai.service import analyze


async def test_daily_states(repo, settings):
    source = repo / "src" / "retry.py"
    source.write_text("def retry():\n    return 2\n", encoding="utf-8")
    git(repo, "add", "src/retry.py")
    source.write_text("def retry():\n    return 3\n", encoding="utf-8")
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")
    before = git(repo, "status", "--porcelain")
    for mode, old, new, count in [
        ("staged", "1", "2", 1),
        ("unstaged", "2", "3", 2),
        ("combined", "1", "3", 2),
    ]:
        result = await analyze("diff", DiffRequest(mode=mode, profile="off"), settings=settings)
        assert result.statistics["changed_files"] == count
        directory = (
            __import__("pathlib").Path(settings.state_dir) / "runs" / result.run_id / "evidence"
        )
        patches = "\n".join(p.read_text("utf-8") for p in directory.glob("*.json"))
        assert f"-    return {old}" in patches
        assert f"+    return {new}" in patches
    assert git(repo, "status", "--porcelain") == before


async def test_committed_modes_and_related_tests(repo, settings):
    initial = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "retry.py").write_text("def retry():\n    return 99\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")
    for mode in ("direct", "merge_base"):
        result = await analyze(
            "diff", DiffRequest(mode=mode, base_ref=initial, profile="off"), settings=settings
        )
        assert result.statistics["changed_files"] == 1
        assert any("test_retry" in f.text for f in result.facts)
        assert result.model.used is False


async def test_invalid_ref_safe(repo, settings):
    result = await analyze(
        "diff",
        DiffRequest(mode="direct", base_ref="--upload-pack=bad", profile="off"),
        settings=settings,
    )
    assert result.status == "failed"
    assert any("INVALID_REF" in item for item in result.limitations)


async def test_no_changes(repo, settings):
    result = await analyze("diff", DiffRequest(profile="off"), settings=settings)
    assert result.statistics["changed_files"] == 0


async def test_git_external_helper_never_runs(repo, settings):
    marker = repo / "helper-ran"
    git(repo, "config", "diff.external", f"touch {marker}")
    (repo / "src" / "retry.py").write_text("return 42", encoding="utf-8")
    result = await analyze("diff", DiffRequest(profile="off"), settings=settings)
    assert result.status != "failed"
    assert not marker.exists()


async def test_unborn_checkout(tmp_path, monkeypatch, settings):
    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init")
    (root / "new.py").write_text("x = 1", encoding="utf-8")
    git(root, "add", ".")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    result = await analyze("diff", DiffRequest(mode="staged", profile="off"), settings=settings)
    assert result.statistics["changed_files"] == 1
