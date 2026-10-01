import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from datetime import datetime

from .evidence import Run
from .files import Budget, contained, walk
from .project import terms
from .schemas import LogRequest, WorkerError
from .secrets import secret_container

TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?")
LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)\b", re.I)
CORRELATION = re.compile(
    r"\b((?:request|trace|call|correlation|operation|span)[_-]?id)\s*[:=]\s*[\"']?([\w.-]+)", re.I
)
CONTINUATION = re.compile(
    r"^(?:\s+|Traceback|Caused by:|During handling|[\w.]+(?:Error|Exception):|\s*at |\.\.\.)"
)


def timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = TIMESTAMP.search(value)
    if not match:
        return None
    try:
        parsed = datetime.fromisoformat(match.group().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def filter_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = timestamp(value)
    if parsed is None:
        raise WorkerError("TIME_FILTER", "Time filters need a timestamp with an explicit timezone")
    return parsed


def records(stream, budget: Budget, max_event: int):
    """Yield bounded raw event text and exact physical line coordinates."""
    pending, start, number, pending_bytes = [], 1, 0, 0
    for raw in iter(lambda: stream.readline(max_event + 1), b""):
        budget.charge(len(raw))
        number += 1
        if len(raw) > max_event:
            # Consume the remainder of this physical line without treating chunks as events.
            while not raw.endswith(b"\n"):
                raw = stream.readline(max_event + 1)
                if not raw:
                    break
                budget.charge(len(raw))
            budget.skipped["oversized_event"] += 1
            pending, pending_bytes = [], 0
            continue
        if b"\0" in raw:
            budget.skipped["binary_line"] += 1
            continue
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        continuation = bool(
            pending
            and CONTINUATION.match(line)
            and not TIMESTAMP.match(line.strip())
            and not LEVEL.match(line.strip())
            and not line.lstrip().startswith("{")
        )
        if continuation:
            if pending_bytes + len(raw) > max_event:
                budget.skipped["oversized_event"] += 1
                pending, pending_bytes = [], 0
                continue
            pending.append(line)
            pending_bytes += len(raw)
        else:
            if pending:
                yield "\n".join(pending), [start, number - 1]
            pending, start, pending_bytes = [line], number, len(raw)
    if pending:
        yield "\n".join(pending), [start, number]


def json_records(raw: str):
    """Top-level JSON arrays with exact member spans, or one JSON document."""
    decoder = json.JSONDecoder()
    pos = len(raw) - len(raw.lstrip())
    if pos < len(raw) and raw[pos] == "[":
        pos += 1
        while pos < len(raw):
            while pos < len(raw) and raw[pos] in " \r\n\t,":
                pos += 1
            if pos >= len(raw) or raw[pos] == "]":
                break
            _, end = decoder.raw_decode(raw, pos)
            yield raw[pos:end], [raw.count("\n", 0, pos) + 1, raw.count("\n", 0, end) + 1]
            pos = end
    else:
        decoder.raw_decode(raw, pos)
        yield raw, [1, raw.count("\n") + 1]


def normalize(text: str) -> str:
    text = TIMESTAMP.sub("<time>", text)
    text = CORRELATION.sub(r"\1=<id>", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", text, flags=re.I)
    text = re.sub(r"(?<=line )\d+|(?<=:)\d+(?=\b)|\b0x[0-9a-f]+\b", "<n>", text, flags=re.I)
    return text


def event_details(raw: str, redactor):
    try:
        parsed = json.loads(raw)
        structured = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        structured = {}
    safe, changed = redactor.screen(raw)
    flat = {}

    def flatten(value, prefix="", depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, dict):
                    flatten(item, prefix + str(key) + ".", depth + 1)
                else:
                    flat[str(key).lower()] = item

    flatten(redactor.object(structured))
    time_value = next(
        (flat[k] for k in ("timestamp", "time", "@timestamp", "ts") if k in flat), safe
    )
    when = timestamp(time_value)
    level = str(flat.get("level", flat.get("severity", ""))).upper()
    level_match = LEVEL.search(safe)
    level = level or (level_match.group().upper() if level_match else "UNKNOWN")
    message = str(flat.get("message", flat.get("msg", safe)))
    # Include structured stack/exception fields to keep distinct failures distinct.
    for key in ("stack", "stacktrace", "exception", "error"):
        if key in flat:
            message += "\n" + str(flat[key])
    ids = [
        (key, str(value))
        for key, value in flat.items()
        if re.fullmatch(r"(?:request|trace|call|correlation|operation|span)[_-]?id", key, re.I)
    ]
    ids.extend((key.lower(), value) for key, value in CORRELATION.findall(safe))
    return safe, changed, when, level, message, sorted(set(ids))


def analyze(run: Run, settings, request: LogRequest, budget: Budget):
    since, until = filter_time(request.since), filter_time(request.until)
    if since and until and since > until:
        raise WorkerError("TIME_FILTER", "since must be earlier than until")
    paths = set()
    for selected in request.paths:
        for path in walk(run.root, selected, settings.max_entries, logs=True):
            paths.add(path)
            if len(paths) >= settings.max_entries:
                run.limit("Log file inventory reached its entry limit.")
                break
    groups, levels = {}, Counter()
    correlations = defaultdict(
        lambda: {"count": 0, "events": deque(maxlen=8), "markers": Counter()}
    )
    total = included = unknown_time = filtered = 0
    earliest = latest = None
    for path in sorted(paths):
        if secret_container(path):
            budget.skipped["credential_container"] += 1
            continue
        try:
            budget.check()
            filename = contained(run.root, path)
            if not filename.is_file():
                continue
            with filename.open("rb") as stream:
                if filename.suffix.lower() == ".json":
                    raw = stream.read(
                        min(settings.max_log_bytes - budget.bytes, 4 * 1024 * 1024) + 1
                    )
                    budget.charge(len(raw))
                    if len(raw) > 4 * 1024 * 1024:
                        run.limit("A JSON document exceeds 4 MiB; use JSONL for larger logs.")
                        continue
                    try:
                        events = list(json_records(raw.decode("utf-8")))
                    except (ValueError, UnicodeDecodeError):
                        budget.skipped["malformed_json"] += 1
                        continue
                else:
                    events = records(stream, budget, settings.max_event_bytes)
                for raw_event, lines in events:
                    total += 1
                    if len(raw_event.encode("utf-8")) > settings.max_event_bytes:
                        budget.skipped["oversized_event"] += 1
                        continue
                    safe, redacted, when, level, message, ids = event_details(
                        raw_event, run.redactor
                    )
                    if when is None:
                        unknown_time += 1
                    if (since or until) and (
                        when is None or since and when < since or until and when > until
                    ):
                        filtered += 1
                        continue
                    included += 1
                    levels[level] += 1
                    if when:
                        earliest = min(earliest, when) if earliest else when
                        latest = max(latest, when) if latest else when
                    normalized = normalize(message)
                    key = (level, hashlib.sha256(normalized.encode("utf-8")).hexdigest())
                    if key not in groups:
                        if len(groups) >= 2000:
                            budget.skipped["group_limit"] += 1
                            continue
                        groups[key] = {
                            "id": f"g{len(groups) + 1}",
                            "count": 0,
                            "level": level,
                            "message": normalized[:4096],
                            "sample": (safe, path, lines),
                            "first": None,
                            "last": None,
                        }
                    group = groups[key]
                    group["count"] += 1
                    if when:
                        group["first"] = min(group["first"], when) if group["first"] else when
                        group["last"] = max(group["last"], when) if group["last"] else when
                    for key_id in ids:
                        if key_id not in correlations and len(correlations) >= 5000:
                            budget.skipped["correlation_limit"] += 1
                            continue
                        correlation = correlations[key_id]
                        correlation["count"] += 1
                        for kind, pattern in (
                            ("start", r"\b(?:start(?:ed|ing)?|begin|received)\b"),
                            ("finish", r"\b(?:complet(?:e|ed)|finish(?:ed)?|succeeded)\b"),
                            ("failure", r"\b(?:fail(?:ed|ure)?|timeout|exception)\b"),
                        ):
                            if re.search(pattern, message, re.I):
                                correlation["markers"][kind] += 1
                        correlation["events"].append(
                            (when.isoformat() if when else None, group["id"], safe, path, lines)
                        )
        except OSError:
            budget.skipped["unreadable_file"] += 1
        except WorkerError as error:
            if error.code in {"DEADLINE", "BYTE_LIMIT"}:
                run.limit(str(error) + "; counts cover only the parsed scope.")
                break
            budget.skipped[error.code.lower()] += 1
    run.result.statistics.update(
        files_considered=len(paths),
        events_parsed=total,
        events_included=included,
        events_filtered=filtered,
        events_unknown_time=unknown_time,
        groups=len(groups),
        correlation_ids=len(correlations),
        bytes_read=budget.bytes,
    )
    for level, count in levels.items():
        run.result.statistics[f"level_{level.lower()}"] = count
    summary = {
        "events_included": included,
        "groups": len(groups),
        "levels": dict(levels),
        "first_timestamp": earliest.isoformat() if earliest else None,
        "last_timestamp": latest.isoformat() if latest else None,
    }
    eid = run.add(json.dumps(summary, indent=2), None, "inventory")
    run.fact(
        f"Parsed {total} events; {included} matched the selected scope; grouped them into {len(groups)} normalized patterns.",
        [eid],
    )
    if earliest:
        run.fact(
            f"Known timestamps range from {earliest.isoformat()} to {latest.isoformat()}.", [eid]
        )
    run.log_groups = {}
    ranked = sorted(
        groups.values(),
        key=lambda g: (
            -sum(t in g["message"].lower() for t in terms(request.query)),
            g["level"] not in {"ERROR", "FATAL", "CRITICAL"},
            -g["count"],
            g["id"],
        ),
    )
    for group in ranked[:20]:
        sample, path, lines = group["sample"]
        sample_id = run.add(sample, path, "log", lines=lines)
        metadata_id = run.add(
            json.dumps(
                {
                    k: str(v) if isinstance(v, datetime) else v
                    for k, v in group.items()
                    if k != "sample"
                },
                indent=2,
            ),
            None,
            "inventory",
        )
        run.log_groups[group["id"]] = {"count": group["count"], "evidence": sample_id}
        run.fact(
            f"{group['id']}: {group['level']} pattern occurred {group['count']} times.",
            [sample_id, metadata_id],
        )
    for (key, value), correlation in sorted(
        correlations.items(), key=lambda pair: -pair[1]["count"]
    )[:5]:
        events = list(correlation["events"])
        # Unknown time remains input-ordered; known timestamps sort for a timeline.
        if all(item[0] for item in events):
            events.sort(key=lambda event: datetime.fromisoformat(event[0]))
        references = []
        for when, group_id, content, path, lines in events[:8]:
            references.append(run.add(content, path, "log", lines=lines))
        run.fact(
            f"{key}={value} links {correlation['count']} events; selected timeline contains {len(references)} events.",
            references,
        )
        if correlation["markers"]:
            run.fact(
                f"Observed lifecycle markers for {key}={value}: "
                + ", ".join(f"{kind}={count}" for kind, count in correlation["markers"].items())
                + ".",
                references,
            )
    if unknown_time:
        run.limit(
            f"{unknown_time} events lack an unambiguous timezone-aware timestamp; input order is preserved, and time filters exclude them."
        )
    if len(groups) > 20:
        run.limit(
            f"Model/evidence selection represents 20 of {len(groups)} groups; deterministic counts cover the parsed scope."
        )
    run.note(
        "Grouping and lifecycle/correlation inference use common formats; model root causes remain hypotheses."
    )
    if budget.skipped:
        run.limit(
            "Skipped input: "
            + ", ".join(f"{key}={count}" for key, count in sorted(budget.skipped.items()))
        )
