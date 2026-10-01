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

The worker uses a nonblocking cross-process inference lock. A busy worker or a
conflicting model resident from another application triggers immediate deterministic
fallback. Switching worker models unloads the previous one first. It does not unload
another application's model. Ollama clients outside this tool are not coordinated.

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
local-ai clean                        # removes completed worker run directories
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
MCP tool or long-term evidence database. Clean old runs explicitly with `local-ai clean`.

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

Authentication source code is allowed. Supported screening covers named password,
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
uv run ruff check src tests
uv build
```

Tests use unrelated fixture repositories, real Git and a real stdio MCP subprocess,
with mocked Ollama. No live model or external network is required for tests.

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
