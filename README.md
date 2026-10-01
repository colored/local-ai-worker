# Local AI Worker

A standalone read-only worker for Claude Code: repository briefings, daily Git
diff analysis, and log triage. Deterministic code selects and counts evidence;
Ollama proposes cited hypotheses. Claude remains the primary reviewer.

## Install once on macOS

Requires Python 3.12+, `uv`, Git supporting `--no-lazy-fetch`, and optionally Ollama.
The code runs outside the project you want analyzed.

```bash
uv tool install /path/to/local_ai_worker-0.1.0-py3-none-any.whl
# Or install from this standalone checkout:
uv tool install /path/to/local-ai-worker

claude mcp add --transport stdio --scope user local-ai \
  -- "$(command -v local-ai)" serve
```

Start Claude normally inside any repository. The four tools automatically use
`CLAUDE_PROJECT_DIR`; launching inside a component finds its containing Git root.
No per-repository files, allowlist entries, or repeated root arguments are required.
Explicit MCP root overrides are restricted to the current project and optional
`additional_roots` in user configuration. The CLI also accepts `--repo`.

## Enable local inference once

Deterministic analysis works immediately. To enable inference, first configure
the **Ollama daemon** with `disable_ollama_cloud: true` in its `~/.ollama/server.json`
or `OLLAMA_NO_CLOUD=1` in its daemon environment, then restart it. Preserve any
other existing server settings. A worker environment variable does not change an
already-running daemon.

Run `local-ai doctor` to find the worker configuration path. On macOS it defaults
to `~/Library/Application Support/local-ai-worker/config.toml`. Create this once:

```toml
local_only_confirmed = true  # Confirms the daemon configuration above.
keep_alive = "5m"
managed_model_switching = false # Opt in to waiting for conflicting models to expire.
retention_days = 7              # 0 prunes inactive runs at the next maintenance operation.
max_state_bytes = 268435456     # 256 MiB of state file payloads, including temporary writes.
sensitive_path_patterns = ["company-vault/*", "*.credentials"] # Adds to built-in denies.

# Defaults are already provided; overrides are optional.
[profiles.fast]
model = "gemma3:12b"
deadline_seconds = 120
context_tokens = 16384
output_tokens = 2048

[profiles.quality]
model = "gemma3:27b"
deadline_seconds = 180

[profiles.reasoning]
model = "nemotron-3-nano:30b"
deadline_seconds = 240
```

Use `ollama list` to confirm installed models. Missing models cause deterministic
fallback; the worker never downloads them. The default endpoint is
`http://127.0.0.1:11434`. Cloud URLs, proxies, redirects, and cloud model tags are
rejected. The confirmation is an operator assertion, not proof of daemon settings.

Only local Gemma, Llama, and Nemotron tags are accepted in user settings. The built-in
Gemma 3 and Nemotron 3 profiles include offline tokenizers. Another model needs a
matching local `tokenizer = "/absolute/path/tokenizer.json"` in its profile. Tokenizer
assets and their upstream terms are documented in
`src/local_ai/tokenizers/NOTICE.md`; no model weights are bundled.

The worker uses a nonblocking cross-process inference lock. A busy worker or any
conflicting resident model triggers immediate deterministic fallback by default.
`resident.json` is not ownership evidence and is neither read nor written. Ollama
has no client ownership/lease API; even the same model may be used by another client.
The worker never sends unload requests. `keep_alive = "0"` is accepted for config
compatibility but omitted from requests, with a limitation note, to avoid unloading
a shared model. Other values retain the existing idle-duration behavior.

With `managed_model_switching = true`, the worker waits for conflicting models to
expire naturally, polling the loopback resident-model API within the existing run
deadline. It then uses the requested installed model. If the conflict persists it
returns deterministic results. This opt-in never authorizes eviction. External
clients are not coordinated: another client can load a model after the final check,
so the worker cannot promise exclusive residency or a global memory limit.

## Daily use

In Claude, ask normally:

- "Explain the retry component and prioritize files I should read."
- "Analyze my current changes and tell me what may be affected."
- "Review the staged changes."
- "Triage artifacts/run-123 and find likely causes."
- "Show the evidence for that hypothesis."

MCP tools:

```text
inspect_project(focus?, profile="fast", repo_root?)
analyze_diff(mode="combined", base_ref?, head_ref="HEAD", focus?, profile="fast", repo_root?)
analyze_logs(paths, query?, since?, until?, profile="fast", repo_root?)
get_evidence(run_id, evidence_id, cursor?)
```

Profiles: `fast`, `quality`, `reasoning`, and `off`. Results include structured
facts, hypotheses, limitations, evidence locations, and deterministic statistics,
plus a short readable briefing. Each hypothesis cites supplied evidence IDs.

The standalone CLI invokes the same functions:

```bash
local-ai inspect --focus "background job retries"
local-ai diff                         # HEAD to working tree, plus untracked files
local-ai diff --mode unstaged         # index to working tree, plus untracked files
local-ai diff --mode staged           # HEAD to index
local-ai diff --mode direct --base HEAD~1 --head HEAD
local-ai diff --mode merge-base --base main
local-ai logs artifacts/run-123 --query "failed requests"
local-ai logs artifacts/run-123 --since 2026-10-01T08:00:00Z
local-ai evidence RUN_ID e2
local-ai inspect --profile off --json
local-ai doctor
local-ai clean                        # removes inactive validated worker run directories
```

`query` prioritizes log groups and guides interpretation; it is not an exact-text
filter that would discard relevant preceding events. `since` and `until` are filters
and require explicit timezones. Unknown/ambiguous timestamps are counted and excluded
from time-filtered results. Multiline traces stay together. Correlation windows retain
up to eight recent events per identifier and report the full observed count.

Facts describe checked observations; hypotheses need Claude's review. Matching a
filename or reference does not establish a complete call graph or test coverage.
Exact log counts and semantic-cluster membership counts are calculated in code.

## Evidence, limits, and safety

Run directories live in the worker user-data directory, outside the target checkout.
They contain redacted selected excerpts, a result, and a local project locator.
`get_evidence` returns the captured excerpt, including after a restart; it does not
reread modified source. Lines are one-based; patches preserve their original hunk
headers. Large excerpts have an optional continuation cursor. There is no run-management
MCP tool or long-term evidence database.

Runs start with a versioned ownership manifest marked `incomplete`. Completion is
recorded only after an atomic result write. Cancellation/interruption updates the
manifest after collection has stopped; hard crashes leave it incomplete. Public
result schemas remain unchanged. An OS lock distinguishes active runs from stale
incomplete ones and is released automatically on process exit.

`local-ai clean` removes inactive validated runs, including stale incomplete runs.
Automatic maintenance runs before creation and state writes: inactive runs older
than `retention_days` are pruned, then oldest inactive runs are pruned as needed for
`max_state_bytes`. Active runs are never pruned. Evidence writes reserve 32 KiB for
finalization; if quota cannot be met, the write fails with `STATE_QUOTA`. The limit
counts state file payload sizes (including unknown files), not filesystem block
allocation. Multiple active runs or external state changes can still prevent final
writes; the manifest remains safely incomplete in that case.

Cleanup requires a matching worker marker, run ID, timestamp, and known file layout.
It rejects symlinks, junctions, hardlinked files, special files, and linked state
ancestors. Unknown, malformed, and legacy manifests are preserved for manual review;
this includes a crash before the initial manifest could be written. Only validated
run directories can be deleted. State must be private to the worker's OS user.
Deletion uses Python's descriptor-based, symlink-resistant `rmtree`; platforms lacking
it (including Windows) fail closed: `clean` reports `CLEAN_UNSUPPORTED`, automatic
pruning is disabled, and quota enforcement refuses writes when space runs out.
A configured state path must not contain symbolic links.

Default limits: 50,000 project entries, 1 MiB per source file, 32 MiB source reads,
128 MiB log reads, 256 KiB per event, 4 MiB per JSON document (use JSONL for larger
logs), 2 MiB retained evidence, 24 KiB structured results, and 8 KiB evidence pages.
Collection has a 30-second ceiling; total analysis ceilings are 120/180/240 seconds.
Model context defaults to 16K tokens, reserving space for schema, formatting, and
output. Selection uses offline tokenizers; omissions are reported.

Read-only guarantees:

- No target-repository writes, build/test execution, package installation, or arbitrary commands.
- No Git writes, fetches, GitHub calls, credential helpers, external diff/textconv, or clean/process filters.
- Fixed read-only Git arguments; system/global Git config is disabled. Put required line-ending settings in local Git config or `.gitattributes` if necessary.
- Project-contained file reads; symlinks and special/binary files are skipped.
- No worker network requests except loopback Ollama, with proxies and redirects disabled.
- Source/log content and instruction files are data, never worker instructions.
- Strict model JSON validation, evidence-ID checks, and input/output secret screening.

Credential paths are denied before source reads, Git patch/blob reads, and log
reads. Defaults include `.env` and `.env.*` (including example files), `.ssh`, `.aws`,
`.azure`, `.gnupg`, `.kube`, `.docker`, Google Cloud credential directories, common
credential files, and private-key/keystore extensions. `sensitive_path_patterns`
adds case-insensitive shell globs over project-relative paths or individual path
components; separators are normalized and `*` can match `/`. Built-in denies cannot
be disabled. Denied paths are omitted from project and diff inventories/counts.
Ordinary authentication source such as `src/auth.py` and `src/authentication.ts`
remains eligible. Regex redaction still screens all admitted content as defense
in depth; path policy cannot identify credentials stored under arbitrary names.

Supported screening covers named password,
token, API-key, cookie/session fields, authorization headers, common credential token
formats, private-key blocks, and credential-bearing URLs/connection strings. Extra
bounded regex patterns can be configured with `extra_secret_patterns`. Screening does
not recognize every arbitrary or encoded secret. Screening failures withhold content.
Raw secrets are not written to run storage or diagnostics.

Normal linked Git worktrees are supported. Bare repositories, submodule gitdir pointers,
external object alternates, and Git config includes are currently rejected. Filters
are disabled for safety, so filter-dependent differences may need direct review.
Windows support is best-effort; macOS is the intended deployment target.

CLI exit codes: `0` completed/partial usable result, `2` invalid input or configuration,
`4` failed analysis, `130` interrupted. Check `status` and `limitations` for partial results.

## Development and verification

```bash
uv sync --locked
uv run pytest -q
uv run ruff check src tests tools
uv build --wheel
```

Tests use unrelated fixture repositories, real Git and a real stdio MCP subprocess,
with mocked Ollama. No live model or external network is required for tests.
GitHub Actions runs locked dependency sync, the full suite, Ruff, and wheel build
on Linux/macOS with Python 3.12/3.13. It installs each wheel into a clean environment
outside the checkout and runs `tools/verify_wheel.py` to load all bundled prompts
and tokenizers, verify tokenizer hashes, check notices/licenses, and report wheel
size. This workflow does not publish releases.

Before everyday use on the target M5 Pro MacBook:

1. Run tests, including symlink tests, on macOS.
2. Register once and open Claude in two unrelated repositories.
3. Exercise inspection, every diff mode, logs, and evidence retrieval.
4. Stop Ollama and verify deterministic results still arrive.
5. Measure cold/warm runs with `--json`: elapsed time, Ollama load duration, prompt
   tokens, generation count/duration, and total inference duration are recorded.
6. Observe Activity Monitor memory pressure, swap growth, and editor/browser
   responsiveness over repeated fast-profile calls. Configure a smaller approved
   Gemma profile if 12B is disruptive; model switching is never automatic.

Live model quality, warm first-token latency, and M5 Pro memory behavior require
target-machine verification. Automated mock tests do not claim those measurements.
