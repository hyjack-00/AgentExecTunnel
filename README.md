# AgentExecTunnel

Public control repository for the execution tunnel.

**v0.2 transport change**: task dispatch and result return now ride on **ntfy.sh** (two fixed topics) instead of two git repos. File uploads still use the `agent_forward` git repo. See [DESIGN.md](DESIGN.md) for the full picture; the v0.1.x PROGRESS history remains in git history on older commits.

## Message plane (ntfy)

Two world-readable topics on `https://ntfy.sh`:

- `agent-forward-285` — submitter → executor, task envelopes
- `agent-backward-285` — executor → submitter, result envelopes

Executor polls `/{topic}/json?poll=1&since=2h` with base 1s cadence and upward jitter capped at `timeout/2` (default 300s / 2 = 150s). Jitter grows on both idle and error; any new envelope resets it. Submitter uses the same primitive (`wait_for(task_id)`) on the backward topic.

Dedup is task_id-keyed and in-memory on both sides. Executor seeds its `seen_ids` on startup from a one-time poll of the backward topic so a restart within the 2h replay window does not re-run already-finished tasks.

The message plane has **no ACK layer** and **no authentication** — if the executor crashes mid-task and restarts within 2h, the task may re-run once; anyone who guesses the topic can inject a task envelope. MVP assumes a trusted environment. Add HMAC signing or a private ntfy instance for production use.

## File plane (GitHub)

File uploads — binaries, source trees — are still transferred via a plain git repo:

- `agent_forward/files/<namespace>/...`

Provisioned once per host by `tools/bootstrap_repos.py`. The executor does not need this repo at all unless the task commands reference uploaded files.

## Envelope shapes

Task (submitter → `agent-forward-285`):

```json
{
  "kind": "task",
  "version": "v0.3",
  "task_id": "20260420T123456Z-abc12345ef01234567890abcd",
  "created_at": "2026-04-20T12:34:56Z",
  "submitter_id": "host:pid",
  "command": "...one plain shell command string...",
  "timeout_seconds": 300,
  "metadata": {}
}
```

**Unified transport** (v0.3): the envelope carries a single `command` string and nothing more. There is no `submit_mode` / `target_host` / routing metadata in the envelope; every flavor of submitter (direct relay, ssh-wrapped, future kubectl/docker/...) renders its own finished command **client-side** and submits it as a plain string. The executor is mode-agnostic — it just runs `task["command"]` via `/bin/sh -c`. `metadata` is an optional audit channel (e.g., `{"ssh_host": "H20"}` for logs).

For ssh-wrapped payloads, the submitter base64-encodes the user's payload and builds a `ssh HOST "bash -c \"$(echo '<b64>' | base64 -d)\""` trampoline so every intermediate shell sees the payload as an atomic literal and **zero quoting layers chew it**. The terminal preview still shows the human-readable `ssh HOST '<payload>'` form — that form is **for humans**, not what goes on the wire.

Result (executor → `agent-backward-285`):

```json
{
  "kind": "result",
  "version": "v0.3",
  "task_id": "...",
  "executor_id": "host:pid",
  "status": "done | failed | stale",
  "started_at": "...", "finished_at": "...",
  "exit_code": 0,
  "stdout_tail": "...≤4KB...",
  "stderr_tail": "...≤4KB...",
  "command_digest": "sha256:...",
  "process_ref": "pid:12345",
  "stale_at": null
}
```

`timeout_seconds` is authoritative and must be set by the submitter. Missing/invalid produces an immediate `failed` result with an explanatory `stderr_tail`.

## Main tools

```bash
# one-time setup (only needed if this host uploads files)
python3 tools/bootstrap_repos.py

# submitter CLIs — all ship a single command string; executor runs it
# via `<executor_shell> -c <command>`. See SKILL.md for when to pick which.
python3 submitter/submit.py 'ls -la /tmp'                # raw, no wrapping
python3 submitter/submit_bash.py 'ls -la /tmp'           # same as submit.py
python3 submitter/submit_gitbash.py 'ls /c/Users/'       # same (Git Bash target)
python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi' # ssh base64 trampoline
python3 submitter/submit_powershell.py 'echo hello'      # powershell -EncodedCommand
python3 submitter/submit_powershell_ssh.py H20 'uname -a'

# upload a file / directory into agent_forward/files/<namespace>/
# (single-submitter only — concurrent pushes have a known sync issue;
#  see PLAN.md §9)
python3 submitter/submit_files.py --name demo --src /path/to/file-or-dir

# executor loop (long-running; survives transient ntfy outages)
python3 executor/run_executor.py

# availability probe and dashboard
python3 tests/availability/probe.py --probe-id gitbash_echo --count 1
python3 tests/availability/report.py --serve
```

## Startup

Prerequisites: `python3` and (optionally) `git`. Only stdlib Python is required for the message plane; `git` is needed only if this host runs `submitter/submit_files.py`.

```bash
git clone <AgentExecTunnel remote> && cd AgentExecTunnel
python3 executor/run_executor.py                 # starts polling ntfy forward topic
```

If this host will also push files, bootstrap the forward repo once:

```bash
python3 tools/bootstrap_repos.py                 # clones agent_forward
```

`.aet-remotes.json` / `AET_FORWARD_REMOTE` / `AET_DATA_BRANCH` override the default forward repo URL for file uploads only.

## Configuration

Settings override any of these via `agent_exec_tunnel.config.Settings` or env:

**Executor shell** (new in v0.3.2 — the executor runs `<executor_shell> -c <task.command>` directly, no cmd.exe / /bin/sh middle layer):
- `executor_shell` — path to the shell binary. Default: `/bin/bash` on Linux, Git Bash on Windows, fallback to `cmd.exe`. Override with env `AET_EXECUTOR_SHELL=<path>`.
- `executor_shell_args` — the `-c`-equivalent flag list. Default: `["-c"]`, or `["/c"]` if `executor_shell` is `cmd.exe`. Override with env `AET_EXECUTOR_SHELL_ARGS="-Command"` (whitespace-split).

**Ntfy authentication** (new in v0.3.3):
- `agent_exec_tunnel.ntfy_transport.NTFY_AUTH_TOKEN` — hardcoded string at the top of `ntfy_transport.py`. Fill in your ntfy access token (e.g. `tk_xxxxxxx`) to attach `Authorization: Bearer <token>` to **every** publish / poll / attachment-fetch. Leave empty for anonymous ntfy.sh.
- `AET_NTFY_TOKEN` env var — same effect, takes precedence over the hardcoded default. Useful when you prefer not to commit the token.
- One source of truth: both submitter and executor go through `ntfy_transport`, so switching modes is a single-line edit.

**Submitter hardening** (new in v0.3.4):
- `AET_SHOW_WIRE=1` — every `submit_*` CLI emits an extra `[wire] <full_command>` line alongside the three human-readable preview lines, so you can see exactly what goes on the wire without inspecting ntfy. Off by default.
- ssh host validation rejects leading `-` (ssh option-injection guard) and restricts the charset to `[A-Za-z0-9._@:-]+`. Rejected hosts raise `ValueError` before any ntfy publish.
- ARG_MAX pre-flight: every submitter renderer refuses payloads > 100 KB with a message pointing to `submitter/submit_files.py` for out-of-band upload.
- Remote base64 trampoline (used by `submit_gitbash_ssh.py` and `submit_powershell_ssh.py`) now checks `command -v base64` (exits 127 when missing), verifies the decoded payload is non-empty (exits 97 on empty/garbled decode), and `exec`s the decoded command so the remote exit code propagates cleanly.

**Ntfy**:
- `ntfy_server_url` (default `https://ntfy.sh`) — point at a private ntfy instance for auth / throughput
- `ntfy_forward_topic` / `ntfy_backward_topic` — change topic names
- `ntfy_poll_since` (default `"30m"`) — server-side replay window used for dedup bootstrap
- `ntfy_poll_base_seconds` (default `1.0`) — polling base interval
- `ntfy_poll_jitter_growth` (default `1.10`) / `ntfy_poll_jitter_floor` (default `0.05`) — upward jitter shape
- `submit_timeout_grace_seconds` (default `15.0`) — extra wait-budget so the submitter can still see the executor-authored `stale` envelope when a task times out

## Resilience

- **Executor crash mid-task**: on restart, `seen_ids` is seeded from the backward topic's 2h window. Completed tasks do not re-run. Tasks that were still in flight when the executor died are eligible to re-run once if the forward envelope is still in the 2h window — intentional MVP trade-off; add persistent ACK for strict at-most-once.
- **Backward ntfy publish failure**: the executor does not silently drop the result. It spools a `ResultRecord` into `pending_results` and every forward-poll tick retries publish. The task is not marked `seen_ids` until publish succeeds.
- **Forward ntfy poll failure**: jitter grows instead of hammering the server, so a flaky ntfy doesn't turn into a self-DoS loop.
- **Submitter timeout**: `wait_for_result` adds `submit_timeout_grace_seconds` on top of the task timeout so the executor's own stale result envelope has time to arrive. Failure modes differentiate "ntfy unreachable" vs "ntfy reachable but executor silent".

## Availability

`tests/availability/` records probe results into `var/availability/data-YYYYMMDD.jsonl` and renders an HTML dashboard with hop-availability cards, p50/p95/p99 latency, preview/total stage timings, a 24h hourly heartbeat SVG, per-probe table, and recent-failures list.

```bash
python3 tests/availability/probe.py --mode remote_relay --mean-period 300
python3 tests/availability/report.py --serve --host 127.0.0.1 --port 8001
```

`--mode local_relay` sets up a PATH shim so `ssh HOST CMD` is emulated by system bash, letting off-network probes exercise the relay path end to end.

## Process

Every version requires:

- `DESIGN.md` update
- `reviews/vX.Y.md`
- `evaluations/vX.Y.md`
- test/evaluation run

In-flight plan for the current branch lives in [PLAN.md](PLAN.md); the history log up to v0.1.2 is reachable through `git log`.

## Skill

The repository-local skill for the submit UX is:

- [skills/agent-exec-tunnel-submit/SKILL.md](skills/agent-exec-tunnel-submit/SKILL.md)
