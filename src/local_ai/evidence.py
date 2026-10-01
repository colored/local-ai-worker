import json
import os
import re
import uuid
from pathlib import Path

from .schemas import Evidence, EvidencePage, EvidenceRef, Fact, ModelInfo, Result, WorkerError
from .secrets import Redactor


def private_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise WorkerError("STORE_INVALID", "Worker state must not be a symbolic link")
    if os.name != "nt":
        path.chmod(0o700)


def write_json(path: Path, data):
    with path.open("w", encoding="utf-8") as stream:
        if os.name != "nt":
            path.chmod(0o600)
        json.dump(data, stream, ensure_ascii=False)


class Run:
    def __init__(self, state: Path, root: Path, task: str, profile: str, redactor: Redactor):
        self.root, self.redactor = root, redactor
        self.result = Result(run_id=uuid.uuid4().hex, task=task, model=ModelInfo(profile=profile))
        self.directory = state / "runs" / self.result.run_id
        private_dir(self.directory / "evidence")
        write_json(self.directory / "manifest.json", {"root": str(root)})
        self.items: list[Evidence] = []
        self.bytes = 0

    def add(
        self, text: str, path: str | None, source="file", lines=None, revision=None, side=None
    ) -> str:
        text, redacted = self.redactor.screen(text)
        if self.bytes + len(text.encode("utf-8")) > 2 * 1024 * 1024:
            raise WorkerError("EVIDENCE_LIMIT", "Captured evidence limit reached")
        self.bytes += len(text.encode("utf-8"))
        item = Evidence(
            id=f"e{len(self.items) + 1}",
            path=self.redactor.value(path),
            source=source,
            lines=lines,
            revision=revision,
            side=side,
            content=text,
            redacted=redacted,
        )
        self.items.append(item)
        self.result.evidence.append(EvidenceRef(**item.model_dump(exclude={"content", "redacted"})))
        write_json(self.directory / "evidence" / f"{item.id}.json", item.model_dump())
        return item.id

    def fact(self, text: str, evidence_ids: list[str] | None = None):
        self.result.facts.append(
            Fact(
                id=f"f{len(self.result.facts) + 1}",
                text=self.redactor.value(text)[:800],
                evidence_ids=evidence_ids or [],
            )
        )

    def limit(self, message: str):
        self.note(message)
        self.result.status = "partial"

    def note(self, message: str):
        message = self.redactor.value(message)[:400]
        if message not in self.result.limitations:
            self.result.limitations.append(message)

    def finish(self) -> Result:
        # Every externally visible string is screened, including paths and model output.
        result = Result.model_validate(self.redactor.object(self.result.model_dump()))
        while len(result.model_dump_json().encode("utf-8")) > 24 * 1024:
            if result.hypotheses:
                result.hypotheses.pop()
            elif len(result.facts) > 1:
                result.facts.pop()
            elif result.evidence:
                result.evidence.pop()
            else:
                raise WorkerError("RESULT_LIMIT", "Result cannot fit its output limit")
            result.status = "partial"
            marker = (
                "Briefing shortened to the result limit; captured evidence remains available by ID."
            )
            if marker not in result.limitations:
                result.limitations.append(marker)
        cited = {eid for item in [*result.facts, *result.hypotheses] for eid in item.evidence_ids}
        existing = {item.id for item in self.items}
        if not cited <= existing:
            raise WorkerError("INVALID_EVIDENCE", "Result contains invalid evidence references")
        write_json(self.directory / "result.json", result.model_dump())
        return result


def retrieve(
    state: Path,
    run_id: str,
    evidence_id: str,
    redactor: Redactor,
    allowed: list[Path],
    cursor: str | None = None,
) -> EvidencePage:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id) or not re.fullmatch(
        r"e[1-9][0-9]{0,5}", evidence_id
    ):
        raise WorkerError("NOT_FOUND", "Evidence is unavailable")
    directory = state / "runs" / run_id
    try:
        root = Path(json.loads((directory / "manifest.json").read_text("utf-8"))["root"]).resolve()
        if not any(root == p or root.is_relative_to(p) for p in allowed):
            raise WorkerError("ROOT_NOT_ALLOWED", "Evidence belongs to another project")
        item = Evidence.model_validate_json(
            (directory / "evidence" / f"{evidence_id}.json").read_text("utf-8")
        )
    except (OSError, ValueError, KeyError):
        raise WorkerError("NOT_FOUND", "Evidence is unavailable") from None
    text, screened = redactor.screen(item.content)
    try:
        offset = int(cursor or "0")
        if offset < 0 or offset > len(text):
            raise ValueError
    except ValueError:
        raise WorkerError("INVALID_CURSOR", "Evidence cursor is invalid") from None
    end = offset
    size = 0
    while end < len(text):
        width = len(text[end].encode("utf-8"))
        if size + width > 8192:
            break
        size += width
        end += 1
    return EvidencePage(
        **item.model_dump(exclude={"content", "redacted"}),
        run_id=run_id,
        content=text[offset:end],
        redacted=item.redacted or screened,
        next_cursor=str(end) if end < len(text) else None,
    )


def render(result: Result) -> str:
    lines = [f"{result.task}: {result.status} (run {result.run_id})"]
    for label, items in (("Fact", result.facts[:5]), ("Hypothesis", result.hypotheses[:3])):
        for item in items:
            lines.append(f"{label}: {item.text} [{', '.join(item.evidence_ids)}]")
    lines.extend(f"Limitation: {item}" for item in result.limitations[:4])
    text = "\n".join(lines)
    return text.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
