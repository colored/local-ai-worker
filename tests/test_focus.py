from local_ai.schemas import ProjectRequest
from local_ai.service import analyze


async def test_focus_searches_content_beyond_first_eighty_paths(repo, settings):
    for i in range(100):
        (repo / "src" / f"module_{i:03}.py").write_text("def function(): pass\n", encoding="utf-8")
    (repo / "src" / "zz_last.py").write_text(
        "# exceptionalneedle processing\ndef function(): pass\n", encoding="utf-8"
    )
    result = await analyze(
        "project", ProjectRequest(focus="exceptionalneedle", profile="off"), settings=settings
    )
    assert any(e.path == "src/zz_last.py" for e in result.evidence)
