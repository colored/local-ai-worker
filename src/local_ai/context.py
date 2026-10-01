import json
from pathlib import Path

from .schemas import ModelReply, WorkerError

_TOKENIZERS = {}


def tokenizer_for(profile):
    path = (
        Path(profile.tokenizer).expanduser()
        if profile.tokenizer
        else (
            Path(__file__).parent
            / "tokenizers"
            / (
                "gemma3.json"
                if profile.model.startswith("gemma3:")
                else "nemotron3.json"
                if profile.model.startswith("nemotron-3-nano:")
                else "unavailable.json"
            )
        )
    )
    if not path.exists():
        raise WorkerError(
            "TOKENIZER_MISSING", "This model needs an explicitly configured offline tokenizer"
        )
    if str(path) not in _TOKENIZERS:
        from tokenizers import Tokenizer

        _TOKENIZERS[str(path)] = Tokenizer.from_file(str(path))
        _TOKENIZERS[str(path)].no_truncation()
        _TOKENIZERS[str(path)].no_padding()
    return _TOKENIZERS[str(path)]


def assemble(run, profile, query: str | None) -> tuple[list[dict], set[str], dict]:
    tokenizer = tokenizer_for(profile)
    template = (Path(__file__).parent / "prompts" / f"{run.result.task}.txt").read_text("utf-8")
    system = (
        template + "\nReturn only this JSON schema:\n" + json.dumps(ModelReply.model_json_schema())
    )
    intro = json.dumps(
        {
            "query": query,
            "facts": [f.model_dump() for f in run.result.facts],
            "statistics": run.result.statistics,
        },
        ensure_ascii=False,
    )
    fixed = len(tokenizer.encode(system + intro).ids)
    # A separate reserve covers chat-template tokens and variation among installed tags.
    allowance = profile.context_tokens - profile.output_tokens - 2048 - fixed
    if allowance < 512:
        raise WorkerError(
            "CONTEXT_LIMIT", "Instructions and facts leave insufficient model context"
        )
    selected, used, identifiers = [], 0, set()
    omitted = 0
    for item in run.items:
        serialized = json.dumps(item.model_dump(), ensure_ascii=False)
        cost = len(tokenizer.encode(serialized).ids)
        if cost + used > allowance:
            omitted += 1
            continue
        selected.append(serialized)
        identifiers.add(item.id)
        used += cost
    if not selected:
        raise WorkerError("CONTEXT_LIMIT", "No evidence fit the selected model context")
    if omitted:
        run.limit(
            f"Model context omitted {omitted} captured evidence items to fit the token budget."
        )
    user = intro + "\nUNTRUSTED REDACTED EVIDENCE:\n" + "\n".join(selected)
    metrics = {
        "context_tokens_selected": used,
        "context_tokens_fixed": fixed,
        "context_evidence_omitted": omitted,
    }
    return (
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        identifiers,
        metrics,
    )
