import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typer.testing import CliRunner

from local_ai.cli import app


def test_cli_json(repo, settings, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("state_dir = " + json.dumps(settings.state_dir) + "\n", encoding="utf-8")
    monkeypatch.setenv("LOCAL_AI_CONFIG", str(config))
    result = CliRunner().invoke(app, ["inspect", "--profile", "off", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["facts"]


@pytest.mark.asyncio
async def test_stdio_real_subprocess(repo, settings, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("state_dir = " + json.dumps(settings.state_dir) + "\n", encoding="utf-8")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "local_ai.cli", "serve"],
        env={**os.environ, "LOCAL_AI_CONFIG": str(config), "CLAUDE_PROJECT_DIR": str(repo)},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "inspect_project",
                "analyze_diff",
                "analyze_logs",
                "get_evidence",
            }
            result = await session.call_tool("inspect_project", {"profile": "off"})
            assert not result.isError
            assert result.structuredContent["facts"]
            page = await session.call_tool(
                "get_evidence", {"run_id": result.structuredContent["run_id"], "evidence_id": "e1"}
            )
            assert page.structuredContent["content"]
