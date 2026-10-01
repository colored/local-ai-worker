import re
from pathlib import PurePosixPath

from .evidence import Run
from .files import Budget, read_text
from .git import Git
from .project import excerpt, is_test
from .schemas import DiffRequest, WorkerError
from .secrets import secret_container

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
FLAGS = ["--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--no-color", "--no-renames"]


def analyze(run: Run, settings, request: DiffRequest, budget: Budget):
    git = Git(run.root, budget)
    revision = None
    if request.mode in {"direct", "merge_base"}:
        if not request.base_ref:
            raise WorkerError("BASE_REQUIRED", "Committed comparison requires base_ref")
        base, head = git.ref(request.base_ref), git.ref(request.head_ref)
        if request.mode == "merge_base":
            bases = (
                git.run("merge-base", "--all", base, head, allowed_failure=True)
                .decode()
                .splitlines()
            )
            if len(bases) != 1:
                raise WorkerError("MERGE_BASE", "Comparison requires exactly one local merge base")
            base = bases[0]
        args = [base, head]
        revision = head
    else:
        head = (
            git.run("rev-parse", "--verify", "HEAD^{commit}", allowed_failure=True).decode().strip()
            or EMPTY_TREE
        )
        args = (
            ["--cached", head]
            if request.mode == "staged"
            else [head]
            if request.mode == "combined"
            else []
        )
    raw = git.run("diff", *FLAGS, "--name-status", "-z", *args, "--").split(b"\0")
    changes: list[tuple[str, str]] = []
    for index in range(0, len(raw) - 1, 2):
        if raw[index]:
            changes.append((raw[index].decode(), raw[index + 1].decode("utf-8")))
    untracked = set()
    if request.mode in {"unstaged", "combined"}:
        untracked = {
            p.decode("utf-8")
            for p in git.run("ls-files", "-z", "--others", "--exclude-standard").split(b"\0")
            if p
        }
        changes.extend(("untracked", p) for p in sorted(untracked))
    summary = "\n".join(f"{status}\t{path}" for status, path in changes[:200])
    eid = run.add(summary or "No changes in selected state.", None, "inventory")
    run.fact(f"{len(changes)} changed paths in {request.mode} comparison.", [eid])
    run.result.statistics.update(changed_files=len(changes), untracked_files=len(untracked))
    if not changes:
        return
    changed_paths = {p for _, p in changes}
    components = sorted({p.split("/")[0] if "/" in p else "(root)" for p in changed_paths})
    run.fact("Changed top-level groups: " + ", ".join(components[:15]) + ".", [eid])
    test_changes = sum(is_test(p) for p in changed_paths)
    run.fact(f"{test_changes} changed paths match test heuristics.", [eid])
    captured = 0
    for status, path in changes[:60]:
        if secret_container(path):
            run.limit("A credential-container change was withheld.")
            continue
        try:
            if path in untracked:
                text = read_text(run.root, path, budget, settings.max_file_bytes)
                if text is None:
                    continue
                text, lines, _ = excerpt(text, request.focus, 150)
            else:
                patch = git.run("diff", *FLAGS, "--unified=5", *args, "--", path, max_output=262144)
                budget.charge(len(patch))
                text = patch.decode("utf-8", errors="replace")
                if "Binary files" in text:
                    run.limit("Binary changes have metadata only.")
                    continue
                lines = None
                all_lines = text.splitlines()
                if len(all_lines) > 200:
                    text = "\n".join(all_lines[:200])
                    run.limit(
                        "Some large patches were excerpted; inspect the original change for full coverage."
                    )
            run.add(text, path, "diff", lines=lines, revision=revision, side=request.mode)
            captured += 1
        except WorkerError as error:
            if error.code in {"DEADLINE", "BYTE_LIMIT"}:
                raise
            run.limit(f"A changed file was skipped ({error.code}).")
    if len(changes) > 60:
        run.limit("Captured at most 60 changed files.")
    # Literal occurrences and path relations are facts; impact interpretation is left to AI.
    stems = {
        PurePosixPath(p).stem.lower() for p in changed_paths if len(PurePosixPath(p).stem) >= 4
    }
    if revision:
        paths = [
            p.decode("utf-8")
            for p in git.run("ls-tree", "-r", "--name-only", "-z", revision).split(b"\0")
            if p
        ]
    else:
        paths = git.inventory()
    related = sorted(
        (p for p in paths if p not in changed_paths), key=lambda p: (not is_test(p), p)
    )[:200]
    matches = 0
    for path in related:
        if matches >= 12:
            break
        try:
            text = (
                git.blob(path, revision, settings.max_file_bytes)
                if revision or request.mode == "staged"
                else read_text(run.root, path, budget, settings.max_file_bytes)
            )
        except WorkerError as error:
            if error.code in {"DEADLINE", "BYTE_LIMIT"}:
                raise
            continue
        if text is None:
            continue
        words = set(re.findall(r"[\w-]+", text.lower()))
        related_terms = sorted(stems & (words | set(re.findall(r"[\w-]+", path.lower()))))
        if related_terms:
            chunk, lines, _ = excerpt(text, " ".join(related_terms))
            related_id = run.add(
                chunk,
                path,
                lines=lines,
                revision=revision,
                side="index" if request.mode == "staged" else None,
            )
            run.fact(
                f"{'Test candidate' if is_test(path) else 'Reference candidate'} {path} matches changed-name terms: {', '.join(related_terms[:5])}.",
                [related_id],
            )
            matches += 1
    run.result.statistics.update(
        patches_captured=captured, related_candidates=matches, bytes_read=budget.bytes
    )
    run.note(
        "Caller and test associations are bounded filename/literal heuristics, not a complete dependency or coverage analysis."
    )
    run.note(
        "Renames are represented as deletion/addition; staged and working-tree evidence describes the state read during this run."
    )
