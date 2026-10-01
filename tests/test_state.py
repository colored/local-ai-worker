import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from local_ai.evidence import Run
from local_ai.schemas import ProjectRequest, WorkerError
from local_ai.secrets import Redactor
from local_ai.service import analyze
from local_ai.store import clean_runs, state_lock, write_json

safe_cleanup = pytest.mark.skipif(
    not shutil.rmtree.avoids_symlink_attacks, reason="Platform lacks descriptor-based safe deletion"
)


def new_run(settings, tmp_path):
    return Run(
        Path(settings.state_dir),
        tmp_path,
        "project",
        "off",
        Redactor(),
        retention_days=settings.retention_days,
        max_state_bytes=settings.max_state_bytes,
    )


@safe_cleanup
def test_clean_incomplete_completed_but_not_active(settings, tmp_path):
    complete = new_run(settings, tmp_path)
    complete.finish()
    incomplete = new_run(settings, tmp_path)
    incomplete.close()
    active = new_run(settings, tmp_path)
    assert clean_runs(settings) == 2
    assert active.directory.exists()
    active.close()


@safe_cleanup
def test_real_process_crash_releases_lease(settings, tmp_path):
    code = """
import os, sys
from pathlib import Path
from local_ai.evidence import Run
from local_ai.secrets import Redactor
run = Run(Path(sys.argv[1]), Path(sys.argv[2]), 'project', 'off', Redactor())
run.add('captured', 'source.py')
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", code, settings.state_dir, str(tmp_path)])
    assert result.returncode == 17
    manifest = next(Path(settings.state_dir).glob("runs/*/manifest.json"))
    assert json.loads(manifest.read_text())["status"] == "incomplete"
    assert clean_runs(settings) == 1


async def test_cancellation_joins_collector_and_marks_run(settings, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def collect(run, settings, query, budget):
        entered.set()
        release.wait(5)
        budget.check()
        run.add("must not be added", "source.py")

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setattr("local_ai.service.project.inspect", collect)
    task = asyncio.create_task(analyze("project", ProjectRequest(profile="off"), settings=settings))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    directory = next((Path(settings.state_dir) / "runs").iterdir())
    assert json.loads((directory / "manifest.json").read_text())["status"] == "cancelled"
    assert not list((directory / "evidence").iterdir())
    if shutil.rmtree.avoids_symlink_attacks:
        assert clean_runs(settings) == 1


async def test_unexpected_error_marks_incomplete(settings, tmp_path, monkeypatch):
    def crash(*args):
        raise RuntimeError("crash")

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setattr("local_ai.service.project.inspect", crash)
    with pytest.raises(RuntimeError):
        await analyze("project", ProjectRequest(profile="off"), settings=settings)
    manifest = next(Path(settings.state_dir).glob("runs/*/manifest.json"))
    assert json.loads(manifest.read_text())["status"] == "interrupted"


@safe_cleanup
def test_retention_prunes_stale_incomplete(settings, tmp_path):
    old = new_run(settings, tmp_path)
    old.manifest["created"] = time.time() - 8 * 86400
    old.close()
    recent = new_run(settings, tmp_path)
    assert not old.directory.exists()
    assert recent.directory.exists()
    recent.close()


@safe_cleanup
def test_quota_prunes_oldest_and_preserves_unowned(settings, tmp_path):
    settings.max_state_bytes = 65536
    old = new_run(settings, tmp_path)
    old.add("a" * 22000, "source.py")
    old.finish()
    new = new_run(settings, tmp_path)
    new.add("b" * 22000, "source.py")
    assert not old.directory.exists()
    new.finish()
    foreign = Path(settings.state_dir) / "foreign.bin"
    foreign.write_bytes(b"x" * 65536)
    with pytest.raises(WorkerError, match="quota"):
        new_run(settings, tmp_path)
    assert foreign.read_bytes() == b"x" * 65536


def test_active_run_quota_stops_evidence_but_can_finalize(settings, tmp_path):
    settings.max_state_bytes = 65536
    run = new_run(settings, tmp_path)
    with pytest.raises(WorkerError, match="quota"):
        run.add("a" * 40000, "source.py")
    assert not run.items
    run.finish()
    assert (run.directory / "result.json").exists()


@safe_cleanup
@pytest.mark.parametrize(
    "attack",
    ["run_link", "evidence_link", "manifest_link", "hardlink", "unknown_file", "fake_manifest"],
)
def test_cleanup_only_validated_owned_trees(settings, tmp_path, attack):
    run = new_run(settings, tmp_path)
    run.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "keep"
    canary.write_text("keep")
    if attack == "run_link":
        shutil.rmtree(run.directory)
        run.directory.symlink_to(outside, target_is_directory=True)
    elif attack == "evidence_link":
        (run.directory / "evidence").rmdir()
        (run.directory / "evidence").symlink_to(outside, target_is_directory=True)
    elif attack == "manifest_link":
        (run.directory / "manifest.json").unlink()
        (run.directory / "manifest.json").symlink_to(canary)
    elif attack == "hardlink":
        os.link(canary, run.directory / "result.json")
    elif attack == "unknown_file":
        (run.directory / "important.txt").write_text("unowned")
    else:
        write_json(run.directory / "manifest.json", {"root": str(tmp_path)})
    assert clean_runs(settings) == 0
    assert run.directory.exists()
    assert canary.read_text() == "keep"


@safe_cleanup
@pytest.mark.parametrize("component", ["state", "runs", "ancestor"])
def test_cleanup_rejects_linked_ancestors(settings, tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    state = Path(settings.state_dir)
    if component == "state":
        state.symlink_to(outside, target_is_directory=True)
    elif component == "runs":
        state.mkdir()
        (state / "runs").symlink_to(outside, target_is_directory=True)
    else:
        link = tmp_path / "link"
        link.symlink_to(outside, target_is_directory=True)
        settings.state_dir = str(link / "state")
    with pytest.raises(WorkerError, match="links"):
        clean_runs(settings)
    assert not list(outside.iterdir())


def test_state_lock_is_exclusive(settings):
    state = Path(settings.state_dir)
    with state_lock(state):
        with pytest.raises(WorkerError, match="busy"):
            with state_lock(state):
                pytest.fail("Concurrent store mutation admitted")


@safe_cleanup
def test_clean_cli_handles_incomplete_run(settings, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from local_ai.cli import app

    run = new_run(settings, tmp_path)
    run.close()
    monkeypatch.setattr("local_ai.cli.load_settings", lambda: settings)
    result = CliRunner().invoke(app, ["clean"])
    assert result.exit_code == 0
    assert "Removed 1" in result.output


@safe_cleanup
def test_symlink_substitution_at_deletion_cannot_escape(settings, tmp_path, monkeypatch):
    run = new_run(settings, tmp_path)
    run.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "canary").write_text("preserved")
    original = shutil.rmtree

    def swap_then_delete(path, **kwargs):
        run.directory.rename(run.directory.with_name("moved"))
        run.directory.symlink_to(outside, target_is_directory=True)
        original(path, **kwargs)

    swap_then_delete.avoids_symlink_attacks = True
    monkeypatch.setattr(shutil, "rmtree", swap_then_delete)
    with pytest.raises(WorkerError, match="cleaned safely"):
        clean_runs(settings)
    assert (outside / "canary").read_text() == "preserved"


def test_atomic_finalization_failure_leaves_incomplete(settings, tmp_path, monkeypatch):
    run = new_run(settings, tmp_path)

    def full_disk(*args):
        raise OSError("disk full")

    monkeypatch.setattr("local_ai.store.os.replace", full_disk)
    with pytest.raises(OSError):
        run.finish()
    run.close()
    assert not (run.directory / "result.json").exists()
    assert json.loads((run.directory / "manifest.json").read_text())["status"] == "incomplete"
    assert run.lease is None


async def test_dotdot_state_cannot_bypass_project_containment(settings, tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    settings.state_dir = str(root / ".." / "project" / "state")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    with pytest.raises(WorkerError, match="outside"):
        await analyze("project", ProjectRequest(profile="off"), settings=settings)
    assert not (root / "state").exists()
