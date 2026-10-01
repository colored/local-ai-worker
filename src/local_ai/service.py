import asyncio
import logging
import time
from pathlib import Path

from . import diff, logs, project
from .config import load_settings, project_root
from .evidence import Run, retrieve
from .files import Budget
from .ollama import assist
from .schemas import WorkerError
from .secrets import Redactor
from .store import no_links

logger = logging.getLogger("local_ai")


async def analyze(task, request, *, settings=None, mcp=False, transport=None):
    settings = settings or load_settings()
    root = project_root(settings, request.repo_root, mcp=mcp)
    state = Path(settings.state_dir).expanduser().absolute()
    no_links(state)
    state = state.resolve()
    if state == root or state.is_relative_to(root):
        raise WorkerError("STORE_SCOPE", "Worker state must be outside the target project")
    redactor = Redactor(settings.extra_secret_patterns)
    run = Run(
        state,
        root,
        task,
        request.profile,
        redactor,
        retention_days=settings.retention_days,
        max_state_bytes=settings.max_state_bytes,
    )
    query = request.query if task == "logs" else request.focus
    query = redactor.value(query)
    request = request.model_copy(update={"query" if task == "logs" else "focus": query})
    started = time.monotonic()
    profile = settings.profiles.get(request.profile)
    deadline = profile.deadline_seconds if profile else 120
    budget = Budget(
        min(deadline, 30),
        settings.max_log_bytes if task == "logs" else settings.max_source_bytes,
    )
    try:
        # Execute in a thread so the stdio event loop can receive cancellation.
        def operation():
            if task == "project":
                project.inspect(run, settings, query, budget)
            elif task == "diff":
                diff.analyze(run, settings, request, budget)
            else:
                logs.analyze(run, settings, request, budget)

        collector = asyncio.create_task(asyncio.to_thread(operation))
        try:
            await asyncio.shield(collector)
        except BaseException:
            budget.deadline = 0
            # Do not release the run lease or finalize while the collector can write.
            # Repeated cancellation must not detach a still-running collector.
            while not collector.done():
                try:
                    await asyncio.shield(collector)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if collector.done() and not collector.cancelled():
                collector.exception()
            raise
        remaining = deadline - (time.monotonic() - started)
        if remaining > 0 and run.items:
            await assist(run, settings, query, transport=transport, seconds=remaining)
        elif remaining <= 0:
            run.limit("Analysis deadline reached before model assistance.")
    except asyncio.CancelledError:
        budget.deadline = 0
        run.close("cancelled")
        # Collector has stopped before releasing the run lease.
        logger.info("operation=%s run=%s status=cancelled", task, run.result.run_id)
        raise
    except WorkerError as error:
        run.limit(f"{error.code}: {error}")
        if not run.result.facts:
            run.result.status = "failed"
    except (OSError, UnicodeError, ValueError, RecursionError):
        run.limit("Input could not be processed safely; available results are partial.")
        if not run.result.facts:
            run.result.status = "failed"
    except BaseException:
        run.close("interrupted")
        raise
    try:
        run.result.statistics["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result = run.finish()
        logger.info(
            "operation=%s run=%s status=%s elapsed=%.3f",
            task,
            result.run_id,
            result.status,
            time.monotonic() - started,
        )
        return result
    finally:
        run.close()


def evidence(run_id, evidence_id, cursor=None, *, settings=None, mcp=False):
    settings = settings or load_settings()
    root = project_root(settings, mcp=mcp)
    allowed = (
        [root, *(Path(p).expanduser().resolve() for p in settings.additional_roots)]
        if mcp
        else [root]
    )
    return retrieve(
        Path(settings.state_dir).expanduser(),
        run_id,
        evidence_id,
        Redactor(settings.extra_secret_patterns),
        allowed,
        cursor,
    )


async def doctor(settings=None):
    import httpx

    settings = settings or load_settings()
    checks = {
        "config": "valid",
        "root": "available" if project_root(settings) else "missing",
        "local_only_confirmed": settings.local_only_confirmed,
    }
    try:
        async with httpx.AsyncClient(
            base_url=settings.endpoint, trust_env=False, follow_redirects=False, timeout=1
        ) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            installed = {item["name"] for item in response.json().get("models", [])}
        checks["ollama"] = "available"
        checks["profiles"] = {
            key: "installed" if profile.model in installed else "missing"
            for key, profile in settings.profiles.items()
        }
    except (httpx.HTTPError, ValueError, KeyError):
        checks["ollama"] = "unavailable; deterministic operation is ready"
    try:
        from .git import Git

        Git(project_root(settings), Budget(2, 1024)).run("rev-parse", "--is-inside-work-tree")
        checks["git"] = "compatible"
    except WorkerError as error:
        checks["git"] = error.code
    return checks
