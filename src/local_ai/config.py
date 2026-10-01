import os
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_config_path, user_data_path
from pydantic import Field, field_validator

from .schemas import StrictModel, WorkerError


class ModelProfile(StrictModel):
    model: str
    tokenizer: str | None = None
    deadline_seconds: int = Field(default=120, ge=1, le=600)
    context_tokens: int = Field(default=16384, ge=8192, le=32768)
    output_tokens: int = Field(default=2048, ge=256, le=3072)

    @field_validator("model")
    @classmethod
    def local_approved_family(cls, value: str) -> str:
        name = value.lower()
        if not name.startswith(("gemma", "llama", "nemotron")) or any(
            marker in name for marker in ("cloud", "/", " ", "\n")
        ):
            raise ValueError("Only local Gemma, Llama, and Nemotron tags are supported")
        return value


class Settings(StrictModel):
    endpoint: str = "http://127.0.0.1:11434"
    state_dir: str = Field(default_factory=lambda: str(user_data_path("local-ai-worker")))
    additional_roots: list[str] = Field(default_factory=list)
    keep_alive: str = "5m"
    managed_model_switching: bool = False
    retention_days: int = Field(default=7, ge=0, le=36500)
    max_state_bytes: int = Field(default=268435456, ge=65536)
    sensitive_path_patterns: list[str] = Field(default_factory=list, max_length=128)
    # Setting this affirms that the independently running daemon has cloud disabled.
    local_only_confirmed: bool = False
    max_entries: int = Field(default=50000, ge=1, le=1000000)
    max_file_bytes: int = Field(default=1048576, ge=1024, le=10485760)
    max_source_bytes: int = Field(default=33554432, ge=1024, le=268435456)
    max_log_bytes: int = Field(default=134217728, ge=1024, le=1073741824)
    max_event_bytes: int = Field(default=262144, ge=1024, le=1048576)
    extra_secret_patterns: list[str] = Field(default_factory=list, max_length=32)
    profiles: dict[str, ModelProfile] = Field(
        default_factory=lambda: {
            "fast": ModelProfile(model="gemma3:12b"),
            "quality": ModelProfile(model="gemma3:27b", deadline_seconds=180),
            "reasoning": ModelProfile(model="nemotron-3-nano:30b", deadline_seconds=240),
        }
    )

    @field_validator("endpoint")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Ollama must use an HTTP loopback endpoint without credentials")
        _ = parsed.port
        return value.rstrip("/")

    @field_validator("keep_alive")
    @classmethod
    def bounded_idle(cls, value: str) -> str:
        import re

        if value != "0" and not re.fullmatch(r"[1-9][0-9]?[sm]", value):
            raise ValueError("Use 0 or a duration such as 30s or 5m")
        return value


def config_path() -> Path:
    return Path(
        os.environ.get("LOCAL_AI_CONFIG", user_config_path("local-ai-worker") / "config.toml")
    )


def load_settings() -> Settings:
    path = config_path()
    try:
        data = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
        return Settings.model_validate(data)
    except Exception:
        raise WorkerError(
            "CONFIG_INVALID", "User configuration is invalid; run local-ai doctor"
        ) from None


def project_root(settings: Settings, override: str | None = None, *, mcp: bool = False) -> Path:
    default = Path(os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))).resolve()
    # Support launching from a component directory inside a checkout.
    if not (default / ".git").exists():
        for parent in default.parents:
            if (parent / ".git").exists():
                default = parent
                break
    root = Path(override).expanduser().resolve() if override else default
    if not root.is_dir():
        raise WorkerError("INVALID_ROOT", "Project directory does not exist")
    if mcp and override:
        allowed = [default, *(Path(p).expanduser().resolve() for p in settings.additional_roots)]
        if not any(root == item or root.is_relative_to(item) for item in allowed):
            raise WorkerError("ROOT_NOT_ALLOWED", "Explicit project is outside the permitted roots")
    return root
