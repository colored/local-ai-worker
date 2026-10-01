import asyncio
import json
from typing import Annotated

import typer
from pydantic import ValidationError

from . import service
from .config import config_path, load_settings
from .evidence import render
from .schemas import DiffRequest, LogRequest, ProjectRequest, WorkerError

app = typer.Typer(no_args_is_help=True, help="Local read-only AI worker for Claude Code.")
Repo = Annotated[
    str | None,
    typer.Option("--repo", help="Defaults to CLAUDE_PROJECT_DIR or the current directory."),
]
ProfileOpt = Annotated[str, typer.Option("--profile", help="fast, quality, reasoning, or off")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Return structured JSON.")]


def execute(task, request, as_json):
    try:
        request = {"project": ProjectRequest, "diff": DiffRequest, "logs": LogRequest}[task](
            **request
        )
        result = asyncio.run(service.analyze(task, request))
        typer.echo(result.model_dump_json(indent=2) if as_json else render(result))
        if result.status == "failed":
            raise typer.Exit(4)
    except WorkerError as error:
        typer.echo(f"{error.code}: {error}", err=True)
        raise typer.Exit(2) from None
    except ValidationError:
        typer.echo("Invalid request; check --help. Input values were withheld.", err=True)
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


@app.command("inspect")
def inspect(
    focus: str | None = None, profile: ProfileOpt = "fast", repo: Repo = None, json: JsonOpt = False
):
    execute("project", dict(focus=focus, profile=profile, repo_root=repo), json)


@app.command("diff")
def diff(
    mode: str = "combined",
    base: str | None = None,
    head: str = "HEAD",
    focus: str | None = None,
    profile: ProfileOpt = "fast",
    repo: Repo = None,
    json: JsonOpt = False,
):
    execute(
        "diff",
        dict(
            mode=mode.replace("-", "_"),
            base_ref=base,
            head_ref=head,
            focus=focus,
            profile=profile,
            repo_root=repo,
        ),
        json,
    )


@app.command("logs")
def logs(
    paths: Annotated[list[str], typer.Argument()],
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    profile: ProfileOpt = "fast",
    repo: Repo = None,
    json: JsonOpt = False,
):
    execute(
        "logs",
        dict(paths=paths, query=query, since=since, until=until, profile=profile, repo_root=repo),
        json,
    )


@app.command("evidence")
def evidence(run_id: str, evidence_id: str, cursor: str | None = None, json: JsonOpt = False):
    try:
        result = service.evidence(run_id, evidence_id, cursor)
        typer.echo(result.model_dump_json(indent=2) if json else result.content)
        if result.next_cursor and not json:
            typer.echo(f"\nContinue with --cursor {result.next_cursor}")
    except WorkerError as error:
        typer.echo(f"{error.code}: {error}", err=True)
        raise typer.Exit(2) from None


@app.command("doctor")
def doctor():
    try:
        typer.echo(json.dumps(asyncio.run(service.doctor()), indent=2))
        typer.echo(f"User configuration: {config_path()}")
    except WorkerError as error:
        typer.echo(f"{error.code}: {error}", err=True)
        raise typer.Exit(2) from None


@app.command("serve")
def serve():
    """Run the stdio MCP server; stdout is reserved for protocol messages."""
    from .mcp_server import serve as run

    run()


@app.command("clean")
def clean():
    """Remove completed and stale incomplete worker runs; skip active runs."""
    from .store import clean_runs

    try:
        removed = clean_runs(load_settings())
    except WorkerError as error:
        typer.echo(f"{error.code}: {error}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"Removed {removed} captured worker runs.")


if __name__ == "__main__":
    app()
