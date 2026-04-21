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

# submitter CLIs — pick by executor OS:
# Linux executor:
python3 submitter/submit_bash.py 'ls -la /tmp'

# Windows executor (Git Bash):
python3 submitter/submit_gitbash.py 'ls /c/Users/'
python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'

# Windows executor (PowerShell):
python3 submitter/submit_powershell.py 'echo hello'
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

## Ntfy configuration

Settings override any of these via `agent_exec_tunnel.config.Settings`:

- `ntfy_server_url` (default `https://ntfy.sh`) — point at a private ntfy instance for auth / throughput
- `ntfy_forward_topic` / `ntfy_backward_topic` — change topic names
- `ntfy_poll_since` (default `"2h"`) — server-side replay window used for dedup bootstrap
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
