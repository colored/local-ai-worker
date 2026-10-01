# Production-readiness verification — 2026-10-01

Baseline: `9ad3742` (`Implement portable local AI worker`). Public CLI commands,
MCP tools, request/result schemas, deterministic fallback, and read-only target
repository access are preserved. No release is published by this change.

## Verification

- Windows, CPython 3.12.10: `uv sync --locked` passed.
- Windows, CPython 3.12.10: full `pytest -q` passed: **102 passed, 16 skipped**.
  Skips: 15 descriptor-based cleanup cases unsupported by Windows, plus the
  existing source-symlink test because this account lacks symlink privileges.
- Linux (WSL Ubuntu), CPython 3.12.3: `pytest -q tests/test_state.py` passed:
  **21 passed**, including real process exit, cancellation, retention, quota,
  symlink substitution during deletion, hardlinks, and CLI cleanup.
- `ruff check src tests tools` and `git diff --check` passed.
- `uv build --wheel` passed. Wheel: `local_ai_worker-0.1.0-py3-none-any.whl`,
  **8,506,337 bytes (8.11 MiB)**.
- Installed the wheel into a separate environment and ran `tools/verify_wheel.py`
  from outside the checkout. All three prompts, both tokenizers and their hashes,
  each default profile's tokenizer loading, tokenizer notices/terms, and the
  packaged application license passed. Installed CLI help was also checked.
- GitHub Actions now defines Linux/macOS × Python 3.12/3.13 jobs with locked sync,
  full pytest, Ruff, wheel build, clean installation, resource checks, and wheel
  size reporting. Hosted results are separate from the local results above.

## Behavior changes and limits

- New manifests start incomplete; cancellation/interruption is recorded in the
  manifest without changing public result status enums. Finalization uses atomic
  JSON replacement. Cleanup skips active OS leases; crashed processes release
  their leases. Failed finalization leaves a safely incomplete marker.
- Defaults: `retention_days = 7`, `max_state_bytes = 268435456`. Maintenance prunes
  expired, then oldest inactive validated runs. Quota counts state file payloads,
  not disk blocks; evidence writes reserve finalization space. Unreclaimable quota
  stops writes, and concurrent active runs can leave incomplete finalization.
- Cleanup rejects linked state ancestors, symlinks, junctions, hardlinked files,
  special files, unknown layouts, and malformed/legacy ownership manifests. It
  preserves unrecognized state, including crashes before a manifest was created.
  Windows lacks safe descriptor-based recursive deletion: explicit cleanup fails
  closed; automatic pruning is disabled and quota remains enforced.
- No model is explicitly unloaded. `resident.json` is ignored. Another resident
  model causes immediate fallback unless `managed_model_switching = true`, which
  waits for natural expiry within the deadline. Zero keep-alive is omitted to
  avoid unloading a shared model. Uncoordinated clients can still race residency
  checks; exclusive model ownership and global GPU memory bounds are not claimed.
- Built-in credential path denies cannot be disabled. `sensitive_path_patterns`
  adds case-insensitive globs. Denied files are excluded before source/log reads
  and before Git comparisons/blob reads, including name/status discovery. This
  also omits them from project/diff counts. Authentication source remains allowed.
  Regex screening remains in place. Arbitrary names/encodings can still conceal
  secrets; neither paths nor regexes can detect every secret.
- Local full-suite testing was on Windows. Local Linux cleanup tests passed, but
  that environment's Git 2.43 lacks the already-required `--no-lazy-fetch` option.
  macOS and Python 3.13 require the hosted matrix. Live Ollama/model quality and
  memory behavior were not measured; HTTP tests use mock transports.
- No runtime code execution, Git writes, non-loopback networking, telemetry,
  automatic downloads, or cloud inference were added. Development installs and
  fixture Git writes remain test/build activities only.

## Exact changed files

- `.github/workflows/ci.yml`: four-job verification and installed-wheel smoke check.
- `README.md`: configuration, ownership, lifecycle, credential policy, and limits.
- `docs/production-readiness.md`: this verification record.
- `src/local_ai/cli.py`: validated inactive-run cleanup and safe errors.
- `src/local_ai/config.py`: retention, quota, switching, and sensitive-path settings.
- `src/local_ai/diff.py`: credential policy before Git comparisons.
- `src/local_ai/evidence.py`: leases, manifests, atomic/quota-controlled storage.
- `src/local_ai/files.py`: pre-read credential policy and budget check.
- `src/local_ai/git.py`: deny policy before historical/index blob reads.
- `src/local_ai/logs.py`: deny policy before log reads.
- `src/local_ai/ollama.py`: no ownership inference/unloading; opt-in expiry wait.
- `src/local_ai/project.py`: exclude denied paths from project selection.
- `src/local_ai/secrets.py`: built-in and user-configured sensitive path matching.
- `src/local_ai/service.py`: lifecycle coordination and settings propagation.
- `src/local_ai/store.py`: state locks, safe validation/deletion, quota and retention.
- `tests/test_credentials.py`: pre-read guards and end-to-end opaque canaries.
- `tests/test_limits_lifecycle.py`: stale resident marker regression.
- `tests/test_ollama.py`: shared models, stale/corrupt markers, bounded switching.
- `tests/test_state.py`: crash, cancellation, stale runs, quota and cleanup attacks.
- `tools/verify_wheel.py`: installed resources, hashes, license and size checks.
