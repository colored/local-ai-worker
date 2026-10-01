import asyncio
import json
import math
from contextlib import contextmanager
from pathlib import Path

import httpx

from .context import assemble
from .evidence import private_dir
from .schemas import ModelReply, WorkerError
from .store import Lease


@contextmanager
def inference_lock(state: Path):
    private_dir(state)
    try:
        lease = Lease(state / "inference.lock")
    except WorkerError as error:
        if error.code != "STORE_BUSY":
            raise
        yield False
        return
    try:
        yield True
    finally:
        lease.close()


async def bounded_json(client, method: str, url: str, payload=None, cap=524288):
    async with client.stream(method, url, json=payload) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > cap:
                raise WorkerError("MODEL_OUTPUT_LIMIT", "Ollama response exceeded its size limit")
        return json.loads(body)


async def assist(run, settings, query: str | None, *, transport=None, seconds=None):
    name = run.result.model.profile
    if name == "off":
        return
    if not settings.local_only_confirmed:
        run.limit(
            "Ollama assistance is disabled until user configuration confirms the daemon has cloud disabled."
        )
        return
    if name not in settings.profiles:
        run.limit("Requested model profile is not configured; deterministic results returned.")
        return
    state = Path(settings.state_dir).expanduser()
    with inference_lock(state) as acquired:
        if not acquired:
            run.limit("Ollama worker is busy; deterministic results returned.")
            return
        profile = settings.profiles[name]
        try:
            async with asyncio.timeout(seconds or profile.deadline_seconds):
                timeout = httpx.Timeout(profile.deadline_seconds, connect=0.75)
                async with httpx.AsyncClient(
                    base_url=settings.endpoint,
                    timeout=timeout,
                    trust_env=False,
                    follow_redirects=False,
                    transport=transport,
                ) as client:
                    running = await bounded_json(client, "GET", "/api/ps")
                    # Ollama has no client ownership/lease API. A local marker (even
                    # from this process) cannot prove another client is not using a model.
                    # Switching only waits for natural expiry; it never sends an unload.
                    while any(
                        item.get("name") != profile.model for item in running.get("models", [])
                    ):
                        if not settings.managed_model_switching:
                            raise WorkerError(
                                "MODEL_BUSY",
                                "Another Ollama model is resident; deterministic results returned",
                            )
                        await asyncio.sleep(0.25)
                        running = await bounded_json(client, "GET", "/api/ps")
                    available = await bounded_json(client, "GET", "/api/tags")
                    if profile.model not in {
                        item.get("name") for item in available.get("models", [])
                    }:
                        raise WorkerError(
                            "MODEL_MISSING",
                            "Configured model is not installed; no model was downloaded",
                        )
                    messages, ids, metrics = assemble(run, profile, query)
                    run.result.statistics.update(metrics)
                    payload = {
                        "model": profile.model,
                        "messages": messages,
                        "format": ModelReply.model_json_schema(),
                        "stream": False,
                        "options": {
                            "num_ctx": profile.context_tokens,
                            "num_predict": profile.output_tokens,
                            "temperature": 0,
                        },
                    }
                    # keep_alive=0 explicitly unloads even a model shared with another
                    # client. Never request it, including after an initially empty /ps.
                    if settings.keep_alive != "0":
                        payload["keep_alive"] = settings.keep_alive
                    else:
                        run.note("Zero keep_alive was omitted to avoid unloading a shared model.")
                    for attempt in range(2):
                        response = await bounded_json(client, "POST", "/api/chat", payload)
                        if response.get("done_reason") == "length":
                            raise WorkerError(
                                "MODEL_TRUNCATED",
                                "Model output reached its generation limit and was withheld",
                            )
                        try:
                            raw_reply = ModelReply.model_validate_json(
                                response.get("message", {}).get("content", "")
                            )
                            reply = ModelReply.model_validate(
                                run.redactor.object(raw_reply.model_dump())
                            )
                            if any(not set(h.evidence_ids) <= ids for h in reply.hypotheses):
                                raise ValueError("Invalid citation")
                            if run.result.task != "logs" and reply.clusters:
                                raise ValueError("Unexpected clusters")
                            validate_clusters(run, reply)
                        except ValueError:
                            if attempt:
                                raise WorkerError(
                                    "MODEL_INVALID",
                                    "Model output failed schema or evidence validation",
                                ) from None
                            payload["messages"] = messages + [
                                {
                                    "role": "user",
                                    "content": "Return valid schema JSON using only the supplied evidence/group IDs. Do not add fields.",
                                }
                            ]
                            continue
                        run.result.hypotheses = reply.hypotheses
                        for index, h in enumerate(run.result.hypotheses, 1):
                            h.id = f"h{index}"
                        apply_clusters(run, reply)
                        run.result.model.used = True
                        for key in (
                            "load_duration",
                            "prompt_eval_count",
                            "eval_count",
                            "eval_duration",
                            "total_duration",
                        ):
                            value = response.get(key)
                            if (
                                isinstance(value, (int, float))
                                and math.isfinite(value)
                                and value >= 0
                            ):
                                run.result.statistics[f"model_{key}"] = value
                        break
        except asyncio.CancelledError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            run.limit("Ollama deadline reached; deterministic results returned.")
        except httpx.HTTPError:
            run.limit(
                "Ollama is unavailable or rejected the request; deterministic results returned."
            )
        except WorkerError as error:
            run.limit(str(error))
        except (ValueError, OSError, KeyError, TypeError):
            run.limit(
                "Ollama response or local model configuration was invalid; deterministic results returned."
            )


def validate_clusters(run, reply):
    groups = getattr(run, "log_groups", {})
    seen = set()
    for cluster in reply.clusters:
        for group in cluster.group_ids:
            if group not in groups or group in seen:
                raise ValueError("Invalid/overlapping group")
            seen.add(group)


def apply_clusters(run, reply):
    groups = getattr(run, "log_groups", {})
    for cluster in reply.clusters:
        members = [groups[g] for g in cluster.group_ids]
        count = sum(member["count"] for member in members)
        ids = [member["evidence"] for member in members][:8]
        # The suggested name is model-generated; only the membership count is a fact.
        run.fact(
            f"Suggested cluster contains {count} events from groups {', '.join(cluster.group_ids)}.",
            ids,
        )
        from .schemas import Hypothesis

        run.result.hypotheses.append(
            Hypothesis(
                id=f"h{len(run.result.hypotheses) + 1}",
                text=f"Suggested cluster name: {cluster.name}",
                evidence_ids=ids,
            )
        )
