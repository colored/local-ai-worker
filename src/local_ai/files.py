import os
import time
from collections import Counter
from pathlib import Path, PurePosixPath

from .schemas import WorkerError
from .secrets import secret_container

EXCLUDED = {
    ".git",
    "node_modules",
    "vendor",
    "build",
    "dist",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "target",
    ".cache",
}


class Budget:
    def __init__(self, seconds: float, max_bytes: int, sensitive_paths=None):
        self.deadline = time.monotonic() + seconds
        self.max_bytes = max_bytes
        self.bytes = 0
        self.skipped: Counter = Counter()
        self.sensitive_paths = sensitive_paths or []

    def denied(self, path: str) -> bool:
        return secret_container(path, self.sensitive_paths)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise WorkerError("DEADLINE", "Analysis reached its execution deadline")

    def charge(self, size: int):
        self.check()
        if self.bytes + size > self.max_bytes:
            raise WorkerError("BYTE_LIMIT", "Analysis reached its input byte limit")
        self.bytes += size


def contained(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ".." in pure.parts
        or "\0" in normalized
        or ":" in normalized
    ):
        raise WorkerError("PATH_DENIED", "Path must remain inside the active project")
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise WorkerError("SYMLINK_SKIPPED", "Symbolic links are skipped")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise WorkerError("PATH_DENIED", "Path escapes the active project")
    return candidate


def walk(root: Path, start: str = ".", limit: int = 50000, *, logs: bool = False):
    base = contained(root, start)
    if base.is_file():
        yield base.relative_to(root).as_posix()
        return
    if not base.is_dir():
        raise WorkerError("PATH_MISSING", "Selected path does not exist")
    count = 0
    for directory, dirs, files in os.walk(base, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if d != ".git"
            and (logs or d not in EXCLUDED)
            and not (Path(directory) / d).is_symlink()
        )
        for name in sorted(files):
            count += 1
            if count > limit:
                raise WorkerError("ENTRY_LIMIT", "Project inventory limit reached")
            yield (Path(directory) / name).relative_to(root).as_posix()


def read_text(root: Path, path: str, budget: Budget, max_file: int) -> str | None:
    budget.check()
    if budget.denied(path):
        budget.skipped["credential_container"] += 1
        return None
    candidate = contained(root, path)
    if not candidate.is_file():
        budget.skipped["not_regular_file"] += 1
        return None
    if candidate.stat().st_size > max_file:
        budget.skipped["oversized_file"] += 1
        return None
    with candidate.open("rb") as stream:
        raw = stream.read(max_file + 1)
    budget.charge(len(raw))
    if len(raw) > max_file or b"\0" in raw:
        budget.skipped["binary_or_oversized"] += 1
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        budget.skipped["non_utf8"] += 1
        return None
