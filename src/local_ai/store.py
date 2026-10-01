import json
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .schemas import WorkerError

OWNER = "local-ai-worker/run-v1"
RUN_ID = re.compile(r"[0-9a-f]{32}")


def no_links(path: Path):
    """Reject links/reparse points in every existing component, before resolving."""
    for part in reversed((path.absolute(), *path.absolute().parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or stat.S_ISREG(info.st_mode)
            and info.st_nlink != 1
        ):
            raise WorkerError("STORE_INVALID", "Worker state must not contain links")


def private_dir(path: Path):
    no_links(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    no_links(path)
    if os.name != "nt":
        path.chmod(0o700)


def write_json(path: Path, data):
    no_links(path)
    temp = path.parent / (".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Lease:
    def __init__(self, path: Path, *, dir_fd=None):
        if dir_fd is None:
            no_links(path)
            self.stream = path.open("a+b")
        else:
            fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(fd)
                raise WorkerError("STORE_INVALID", "Worker lock is invalid")
            self.stream = os.fdopen(fd, "r+b")
        if not os.fstat(self.stream.fileno()).st_size:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise WorkerError("STORE_BUSY", "Worker state is busy; try again") from None

    def close(self):
        self.stream.close()


@contextmanager
def state_lock(state):
    private_dir(state)
    lease = Lease(state / "state.lock")
    try:
        yield
    finally:
        lease.close()


def tree_size(path):
    """Count allocated file payloads without following links or special files."""
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if not (Path(directory) / d).is_symlink() and not (Path(directory) / d).is_junction()
        ]
        for name in files:
            info = (Path(directory) / name).lstat()
            total += info.st_size
    return total


def validated_run(fd, run_id):
    """Validate ownership and layout using pinned descriptors, never linked paths."""
    try:
        if os.fstat(fd).st_uid != os.getuid():
            return None
        size = 0
        for base, dirs, files, parent in os.fwalk(".", dir_fd=fd, follow_symlinks=False):
            for name in [*dirs, *files]:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                relative = (Path(base) / name).as_posix()
                if relative == "evidence":
                    if not stat.S_ISDIR(info.st_mode):
                        return None
                elif (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not re.fullmatch(
                        r"(?:manifest\.json|result\.json|active\.lock|"
                        r"evidence/e[1-9][0-9]{0,5}\.json|(?:evidence/)?\.tmp-[0-9a-f]{32})",
                        relative,
                    )
                ):
                    return None
                if info.st_uid != os.getuid():
                    return None
                size += info.st_size if stat.S_ISREG(info.st_mode) else 0
        manifest_fd = os.open(
            "manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        try:
            info = os.fstat(manifest_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16384:
                return None
            data = json.loads(os.read(manifest_fd, 16385))
        finally:
            os.close(manifest_fd)
        if not isinstance(data, dict):
            return None
        if (
            data.get("owner") != OWNER
            or data.get("run_id") != run_id
            or data.get("status") not in {"incomplete", "complete", "cancelled", "interrupted"}
            or not isinstance(data.get("created"), (int, float))
            or not 0 < data["created"] <= time.time() + 60
            or not isinstance(data.get("root"), str)
            or not Path(data["root"]).is_absolute()
            or "active.lock" not in os.listdir(fd)
        ):
            return None
        return data, size
    except (OSError, ValueError):
        return None


def open_directory(path):
    # Open each component relative to its pinned parent, rejecting links atomically.
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def prune_locked(state, retention_days, max_state_bytes, *, clean=False, reserve=0, exclude=None):
    runs = state / "runs"
    no_links(runs)
    if not runs.exists():
        return 0
    # Fail closed on platforms without descriptor-based deletion. Automatic
    # writes still enforce quota, but cannot reclaim space on these platforms.
    if not shutil.rmtree.avoids_symlink_attacks:
        if clean:
            raise WorkerError("CLEAN_UNSUPPORTED", "Safe cleanup requires descriptor-based rmtree")
        return 0
    runs_fd = open_directory(runs)
    try:
        candidates = []
        for name in os.listdir(runs_fd):
            if not RUN_ID.fullmatch(name) or name == exclude:
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=runs_fd)
            except OSError:
                continue
            try:
                checked = validated_run(fd, name)
                if checked:
                    candidates.append((checked[0]["created"], name))
            finally:
                os.close(fd)
        removed = 0
        size = tree_size(state)
        for created, name in sorted(candidates):
            if not (
                clean
                or time.time() - created >= retention_days * 86400
                or size + reserve > max_state_bytes
            ):
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=runs_fd)
            except OSError:
                continue
            try:
                checked = validated_run(fd, name)
                if not checked:
                    continue
                try:
                    lease = Lease(Path("active.lock"), dir_fd=fd)
                except (WorkerError, OSError):
                    continue
                try:
                    # Recheck identity immediately before Python's symlink-safe deletion.
                    current = os.stat(name, dir_fd=runs_fd, follow_symlinks=False)
                    opened = os.fstat(fd)
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                        continue
                    shutil.rmtree(name, dir_fd=runs_fd)
                    size -= checked[1]
                    removed += 1
                finally:
                    lease.close()
            finally:
                os.close(fd)
        return removed
    finally:
        os.close(runs_fd)


def clean_runs(settings):
    state = Path(settings.state_dir).expanduser().absolute()
    no_links(state)
    state = state.resolve()
    try:
        with state_lock(state):
            return prune_locked(
                state, settings.retention_days, settings.max_state_bytes, clean=True
            )
    except OSError:
        raise WorkerError("STORE_INVALID", "Worker state could not be cleaned safely") from None


class Store:
    def __init__(self, state, retention_days=7, max_state_bytes=268435456):
        self.state = state.absolute()
        self.retention_days = retention_days
        self.max_state_bytes = max_state_bytes

    def room(self, reserve=0, exclude=None):
        prune_locked(
            self.state, self.retention_days, self.max_state_bytes, reserve=reserve, exclude=exclude
        )
        if tree_size(self.state) + reserve > self.max_state_bytes:
            raise WorkerError(
                "STATE_QUOTA", "Worker state quota reached; clean old runs or raise max_state_bytes"
            )

    def write(self, path, data, *, final=False):
        with state_lock(self.state):
            # Reserve finalization space while collecting. Include temporary-file
            # bytes so atomic replacement also stays within the configured quota.
            size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            self.room(
                size + (0 if final else 32768), path.relative_to(self.state / "runs").parts[0]
            )
            write_json(path, data)
