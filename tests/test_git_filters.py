from conftest import git

from local_ai.schemas import DiffRequest
from local_ai.service import analyze


async def test_clean_filter_not_executed(repo, settings):
    marker = repo / "filter-ran"
    (repo / ".gitattributes").write_text("*.py filter=hostile\n", encoding="utf-8")
    git(repo, "config", "filter.hostile.clean", f"touch '{marker.as_posix()}'")
    git(repo, "config", "filter.hostile.required", "true")
    (repo / "src" / "retry.py").write_text("new code\n", encoding="utf-8")
    result = await analyze("diff", DiffRequest(profile="off"), settings=settings)
    assert result.facts
    assert not marker.exists()
