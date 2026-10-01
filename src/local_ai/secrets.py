"""Bounded format-based screening; this is not universal secret detection."""

import regex

from .schemas import WorkerError

_KEY = r"(?:password|passwd|pwd|api[_-]?key|client[_-]?secret|secret|(?:access|refresh|auth|session)[_-]?token|token|session[_-]?(?:id|key|secret)|cookie|authorization|connection[_-]?string|database[_-]?url|dsn)"
_RULES = [
    # Full private key blocks, including multiline data.
    (
        r"-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
        "PRIVATE_KEY",
    ),
    (
        r"(?im)^\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:[^\r\n]*",
        "AUTH_HEADER",
    ),
    (
        rf"(?i)([\"']?{_KEY}[\"']?\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}}\]<>]+)",
        "FIELD",
    ),
    (
        r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|https?)://[^\s/@:]+:[^\s/@]+@[^\s\"'<>]+",
        "CONNECTION_STRING",
    ),
    (r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/_.=-]{8,}", "AUTH_VALUE"),
    (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "ACCESS_KEY"),
    (
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{10,})\b",
        "API_KEY",
    ),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "JWT"),
    (
        r"(?i)([?&](?:access_token|refresh_token|api_key|password|token|sessionid)=)[^&\s\"'<>]+",
        "QUERY_SECRET",
    ),
]


class Redactor:
    def __init__(self, extra: list[str] | None = None):
        try:
            self.rules = [(regex.compile(pattern), kind) for pattern, kind in _RULES]
            self.rules.extend((regex.compile(p), "CUSTOM") for p in extra or [])
        except regex.error:
            raise WorkerError(
                "REDACTION_INVALID", "A configured secret pattern is invalid"
            ) from None

    def screen(self, text: str) -> tuple[str, bool]:
        original = text
        try:
            for pattern, kind in self.rules:

                def replace(match, kind=kind):
                    # Preserve line coordinates, including multiline PEM material.
                    prefix = match.group(1) if kind in {"FIELD", "QUERY_SECRET"} else ""
                    return prefix + f"[REDACTED:{kind}]" + "\n" * match.group(0).count("\n")

                text = pattern.sub(replace, text, timeout=0.15)
        except (TimeoutError, regex.error):
            raise WorkerError(
                "SCREENING_FAILED", "Content withheld because secret screening failed"
            ) from None
        return text, text != original

    def value(self, text: str | None) -> str | None:
        return self.screen(text)[0] if text is not None else None

    def object(self, value):
        """Screen values recursively without corrupting serialized JSON quoting."""
        if isinstance(value, dict):
            return {
                self.value(str(k)): (
                    "[REDACTED:FIELD]"
                    if regex.fullmatch(_KEY, str(k), flags=regex.I)
                    else self.object(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.object(v) for v in value]
        if isinstance(value, str):
            return self.value(value)
        return value


def secret_container(path: str, extra: list[str] | None = None) -> bool:
    from fnmatch import fnmatchcase
    from pathlib import PurePosixPath

    normalized = path.replace("\\", "/").lower()
    p = PurePosixPath(normalized)
    directories = {
        ".ssh",
        ".aws",
        ".azure",
        ".gcloud",
        ".gnupg",
        ".kube",
        ".docker",
        "credentials",
        "secrets",
        ".secrets",
        ".password-store",
    }
    return (
        "/.config/gcloud/" in f"/{normalized}/"
        or "/.local/share/keyrings/" in f"/{normalized}/"
        or "/.config/op/" in f"/{normalized}/"
        or normalized.endswith(".config/gh/hosts.yml")
        or any(
            part in directories or part == ".env" or part.startswith(".env.") for part in p.parts
        )
        or p.suffix in {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore", ".kdbx"}
        or p.name
        in {
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "credentials",
            "credentials.json",
            "credentials.yml",
            "credentials.yaml",
            "secrets.json",
            "secrets.yml",
            "secrets.yaml",
            "service-account.json",
            "service_account.json",
            "application_default_credentials.json",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".git-credentials",
            ".gitcookies",
            "kubeconfig",
            "auth.json",
            ".boto",
            ".s3cfg",
            ".pgpass",
            ".my.cnf",
            ".vault-token",
        }
        or any(
            fnmatchcase(normalized, pattern.replace("\\", "/").lower())
            or any(fnmatchcase(part, pattern.lower()) for part in p.parts)
            for pattern in extra or []
        )
    )
