from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Profile = Literal["fast", "quality", "reasoning", "off"]
DiffMode = Literal["unstaged", "staged", "combined", "direct", "merge_base"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Fact(StrictModel):
    id: str
    text: str = Field(max_length=800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class Hypothesis(StrictModel):
    id: str
    text: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    next_query: str | None = Field(default=None, max_length=400)


class EvidenceRef(StrictModel):
    id: str
    path: str | None
    source: Literal["file", "diff", "log", "inventory"]
    lines: list[int] | None = None
    revision: str | None = None
    side: str | None = None


class Evidence(EvidenceRef):
    content: str
    redacted: bool = False


class ModelInfo(StrictModel):
    profile: Profile
    used: bool = False


class Result(StrictModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    task: Literal["project", "diff", "logs"]
    status: Literal["complete", "partial", "failed"] = "complete"
    facts: list[Fact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    statistics: dict[str, int | float] = Field(default_factory=dict)
    model: ModelInfo


class ModelReply(StrictModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=8)
    # Optional grouping suggestions refer to deterministic group IDs, never model counts.
    clusters: list["ClusterProposal"] = Field(default_factory=list, max_length=8)


class ClusterProposal(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    group_ids: list[str] = Field(min_length=1, max_length=12)


class EvidencePage(EvidenceRef):
    schema_version: Literal["1"] = "1"
    run_id: str
    content: str
    redacted: bool
    next_cursor: str | None = None


class AnalysisRequest(StrictModel):
    profile: Profile = "fast"
    repo_root: str | None = Field(default=None, max_length=4096)


class ProjectRequest(AnalysisRequest):
    focus: str | None = Field(default=None, max_length=2000)


class DiffRequest(ProjectRequest):
    mode: DiffMode = "combined"
    base_ref: str | None = Field(default=None, max_length=256)
    head_ref: str = Field(default="HEAD", min_length=1, max_length=256)


class LogRequest(AnalysisRequest):
    paths: list[str] = Field(min_length=1, max_length=32)
    query: str | None = Field(default=None, max_length=2000)
    since: str | None = Field(default=None, max_length=100)
    until: str | None = Field(default=None, max_length=100)

    @field_validator("paths")
    @classmethod
    def bounded_paths(cls, paths: list[str]) -> list[str]:
        if any(len(path) > 4096 for path in paths):
            raise ValueError("Path too long")
        return paths


class WorkerError(Exception):
    """Messages are fixed safe descriptions, never unfiltered provider exceptions."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
