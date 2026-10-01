import json
import re
from collections import Counter
from pathlib import PurePosixPath

from .evidence import Run
from .files import EXCLUDED, Budget, read_text
from .git import Git
from .schemas import WorkerError

LANGUAGES = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".c": "C",
    ".cpp": "C++",
}
MANIFESTS = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "Makefile",
    "CMakeLists.txt",
    "Dockerfile",
}


def terms(query: str | None) -> list[str]:
    return sorted(set(re.findall(r"[\w-]{3,}", (query or "").lower())))[:24]


def is_test(path: str) -> bool:
    p = path.lower()
    return any(x in p.split("/") for x in ("test", "tests", "__tests__", "spec")) or bool(
        re.search(r"(?:^|/)(?:test_|.*[._](?:test|spec)\.)|_test\.", p)
    )


def is_doc(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in {".md", ".rst"} or PurePosixPath(path).name in {
        "CODEOWNERS",
        "LICENSE",
    }


def rank(path: str, focus: str | None) -> int:
    name = PurePosixPath(path).name
    score = sum(25 for term in terms(focus) if term in path.lower())
    if name in MANIFESTS:
        score += 18
    if name.lower() in {"readme.md", "agents.md", "claude.md", "codeowners"}:
        score += 15
    if ".github/workflows/" in path or name in {".gitlab-ci.yml", "Jenkinsfile"}:
        score += 12
    if is_test(path):
        score += 7
    if PurePosixPath(path).suffix in LANGUAGES:
        score += 5
    if is_doc(path):
        score += 4
    return score


def excerpt(text: str, focus: str | None, max_lines=100) -> tuple[str, list[int], bool]:
    lines = text.splitlines()
    matched = next(
        (i for i, line in enumerate(lines) if any(t in line.lower() for t in terms(focus))), 0
    )
    start = max(0, matched - 15)
    selected = lines[start : start + max_lines]
    return (
        "\n".join(selected),
        [start + 1, start + max(1, len(selected))],
        len(selected) < len(lines),
    )


def inspect(run: Run, settings, focus: str | None, budget: Budget):
    git = Git(run.root, budget)
    paths = git.inventory()
    if len(paths) > settings.max_entries:
        paths = paths[: settings.max_entries]
        run.limit("Inventory shortened to the configured entry limit.")
    paths = [
        p
        for p in paths
        if not any(part in EXCLUDED for part in PurePosixPath(p).parts) and not budget.denied(p)
    ]
    languages = Counter(
        LANGUAGES[PurePosixPath(p).suffix] for p in paths if PurePosixPath(p).suffix in LANGUAGES
    )
    top = Counter(p.split("/")[0] if "/" in p else "(root)" for p in paths)
    tests = [p for p in paths if is_test(p)]
    manifests = [p for p in paths if PurePosixPath(p).name in MANIFESTS]
    docs = [p for p in paths if is_doc(p)]
    ci = [
        p
        for p in paths
        if ".github/workflows/" in p
        or PurePosixPath(p).name in {".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml"}
    ]
    inventory = {
        "languages_by_file_extension": dict(languages),
        "top_level_groups": dict(top.most_common(30)),
        "manifests": manifests[:30],
        "test_candidates": tests[:30],
        "ci": ci[:20],
        "documentation": docs[:20],
    }
    evidence = run.add(json.dumps(inventory, indent=2), None, "inventory")
    run.fact(
        f"Inventory contains {len(paths)} files; {len(tests)} match test filename/location heuristics.",
        [evidence],
    )
    if languages:
        run.fact(
            "File extensions indicate: "
            + ", ".join(f"{name} ({count})" for name, count in languages.most_common(8))
            + ".",
            [evidence],
        )
    run.fact(
        f"Found {len(manifests)} build/package manifests, {len(ci)} CI files, and {len(docs)} documentation files.",
        [evidence],
    )
    candidates = sorted(paths, key=lambda p: (-rank(p, focus), p))[: 2000 if focus else 80]
    selected = 0
    for path in candidates:
        budget.check()
        try:
            text = read_text(run.root, path, budget, settings.max_file_bytes)
        except WorkerError as error:
            if error.code in {"DEADLINE", "BYTE_LIMIT"}:
                raise
            budget.skipped[error.code.lower()] += 1
            continue
        if text is None:
            continue
        matched = bool(
            focus and any(term in path.lower() or term in text.lower() for term in terms(focus))
        )
        essential = PurePosixPath(path).name in MANIFESTS or is_doc(path) or path in ci
        if focus and not matched and not essential:
            continue
        content, lines, shortened = excerpt(text, focus)
        eid = run.add(content, path, lines=lines)
        selected += 1
        if shortened:
            run.result.statistics["excerpted_files"] = (
                run.result.statistics.get("excerpted_files", 0) + 1
            )
        if matched and len(run.result.facts) < 16:
            run.fact(
                f"Focus terms occur in {path}; the cited section is a candidate for inspection.",
                [eid],
            )
        if PurePosixPath(path).name == "package.json":
            try:
                manifest = json.loads(text)
                deps = sorted(
                    set(manifest.get("dependencies", {})) | set(manifest.get("devDependencies", {}))
                )
                if deps and len(run.result.facts) < 16:
                    run.fact(
                        f"{path} declares dependencies including {', '.join(deps[:12])}.", [eid]
                    )
            except (ValueError, TypeError):
                run.limit("A package manifest could not be parsed.")
        if selected >= 80:
            run.limit("Captured at most 80 relevant project excerpts.")
            break
    run.result.statistics.update(
        files_considered=len(paths), files_selected=selected, bytes_read=budget.bytes
    )
    run.note(
        "Component boundaries and relevance are inferred from paths, manifests, and selected text; this is not a complete code index."
    )
    if len(paths) > len(candidates):
        run.limit(
            f"Candidate scan covered at most {len(candidates)} prioritized files; additional files may contain relevant material."
        )
    if budget.skipped:
        run.limit(
            "Some files were skipped: "
            + ", ".join(f"{k}={v}" for k, v in sorted(budget.skipped.items()))
        )
