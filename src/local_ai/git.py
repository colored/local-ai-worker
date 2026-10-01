import os
import re
import subprocess
import tempfile
from pathlib import Path

from .files import Budget, contained
from .schemas import WorkerError


class Git:
    def __init__(self, root: Path, budget: Budget):
        self.root, self.budget = root, budget
        marker = root / ".git"
        if marker.is_symlink():
            raise WorkerError(
                "GIT_SCOPE", "Linked Git metadata requires an explicitly approved checkout"
            )
        if marker.is_file():
            pointer = marker.read_text("utf-8").strip()
            if not pointer.startswith("gitdir: ") or len(pointer) > 4096:
                raise WorkerError("GIT_SCOPE", "Invalid linked Git metadata")
            metadata = (root / pointer.removeprefix("gitdir: ")).resolve()
            backlink = metadata / "gitdir"
            if (
                not backlink.is_file()
                or Path(backlink.read_text("utf-8").strip()).resolve() != marker.resolve()
            ):
                raise WorkerError("GIT_SCOPE", "Linked metadata is not registered to this checkout")
            common = metadata / "commondir"
            marker = (
                (metadata / common.read_text("utf-8").strip()).resolve()
                if common.is_file()
                else metadata
            )
        if not marker.is_dir():
            raise WorkerError("NOT_A_REPOSITORY", "Run from the Git checkout root")
        if (marker / "objects" / "info" / "alternates").exists():
            raise WorkerError("GIT_SCOPE", "External Git object stores are not supported")
        for item in (marker / "config", marker / "commondir"):
            if item.is_symlink() or item.name == "commondir" and item.exists():
                raise WorkerError("GIT_SCOPE", "External Git metadata is not supported")
        config = (
            (marker / "config").read_text("utf-8", errors="replace")
            if (marker / "config").exists()
            else ""
        )
        if "[include" in config.lower():
            raise WorkerError("GIT_SCOPE", "Git configuration includes are not supported")
        self.filter_overrides = []
        # git diff can execute clean/process filters even with --no-textconv.
        # Enumerate names with a read-only config query, then disable every local driver.
        names = self.run(
            "config", "--name-only", "--get-regexp", r"^filter\.", allowed_failure=True
        ).decode("utf-8", errors="replace")
        if any(
            not re.fullmatch(r"filter\.[A-Za-z0-9_.-]+\.[A-Za-z]+", name)
            for name in names.splitlines()
        ):
            raise WorkerError("GIT_SCOPE", "A Git filter name cannot be disabled safely")
        drivers = {name.rsplit(".", 1)[0] for name in names.splitlines()}
        for driver in drivers:
            self.filter_overrides.extend(
                [
                    "-c",
                    driver + ".clean=",
                    "-c",
                    driver + ".process=",
                    "-c",
                    driver + ".required=false",
                ]
            )

    def run(self, *args: str, max_output: int = 4 * 1024 * 1024, allowed_failure=False) -> bytes:
        self.budget.check()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_ALLOW_PROTOCOL": "",
                "LC_ALL": "C",
            }
        )
        command = [
            "git",
            "--no-pager",
            "--no-optional-locks",
            "--no-lazy-fetch",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "diff.external=",
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            *self.filter_overrides,
            "-C",
            str(self.root),
            *args,
        ]
        # File-backed pipes prevent unbounded in-memory subprocess output. All temporary
        # state is outside the repository, and removed before returning.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env
                )
            except OSError:
                raise WorkerError("GIT_UNAVAILABLE", "Git executable is unavailable") from None
            import time

            try:
                while proc.poll() is None:
                    self.budget.check()
                    if out.tell() > max_output or err.tell() > 65536:
                        raise WorkerError("GIT_OUTPUT_LIMIT", "Git output exceeded its limit")
                    time.sleep(0.01)
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            out.seek(0)
            data = out.read(max_output + 1)
            if len(data) > max_output:
                raise WorkerError("GIT_OUTPUT_LIMIT", "Git output exceeded its limit")
            if proc.returncode and not allowed_failure:
                raise WorkerError(
                    "GIT_FAILED", "Git could not complete the selected read-only operation"
                )
            return data if proc.returncode == 0 else b""

    def ref(self, value: str) -> str:
        if not value or len(value) > 256 or value.startswith("-") or "\0" in value:
            raise WorkerError("INVALID_REF", "Invalid Git reference")
        raw = self.run(
            "rev-parse", "--verify", "--end-of-options", value + "^{commit}", allowed_failure=True
        )
        if not raw:
            raise WorkerError("INVALID_REF", "Commit reference is not available locally")
        return raw.decode("ascii").strip()

    def inventory(self) -> list[str]:
        raw = self.run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        paths = sorted(set(p.decode("utf-8", errors="strict") for p in raw.split(b"\0") if p))
        return paths

    def blob(self, path: str, revision: str | None, max_bytes: int) -> str | None:
        if self.budget.denied(path):
            return None
        contained(self.root, path)
        spec = f"{revision}:{path}" if revision else f":{path}"
        raw = self.run("show", spec, max_output=max_bytes, allowed_failure=True)
        self.budget.charge(len(raw))
        if b"\0" in raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
