# Design

## Overview

`AgentExecTunnel` is a command tunnel: a submitter CLI publishes a shell command to a remote executor and waits for the final result (exit code + stdout/stderr tails). As of **v0.2** the message plane runs on **ntfy.sh** (one forward topic for task envelopes, one backward topic for result envelopes) instead of a pair of git repos. A single `agent_forward` git repo is still used for **file uploads** (binary assets, source trees) that submit_files.py pushes for tasks to consume.

The system is designed for weak networks that flap and recover. Key properties:

- Submitter sends one POST, then polls GET. No state is committed locally.
- Executor runs a single long-running poll loop on the main thread; each claimed task runs on its own worker thread; the worker publishes the result envelope itself.
- Transport failures are either bounded (forward publish — caller decides how to respond) or infinite (backward publish — worker blocks until success, because dropping a finished result is worse than blocking).
- Dedup is in-memory, keyed on `task_id`, seeded from a 2h replay of the backward topic on executor startup so a restart does not re-run tasks whose results are already visible.

## Goals

- Accept multiple concurrent submitters publishing to the forward topic
- Keep one long-running executor alive through transient ntfy outages
- Never silently drop a finalized result
- Keep the protocol surface small: publish a task envelope, publish a result envelope

## Non-Goals

- Protocol-level streaming (stdout/stderr are 4KB tails at the end)
- Strong exactly-once execution (MVP accepts one re-run on mid-task executor crash within the 2h replay window)
- Authentication / encryption on the ntfy topics (MVP assumes trusted environment; world-readable topic)
- Multi-executor coordination against the same topic pair (documented single-executor constraint)

## Architecture

### Topics

- `agent-forward-285` (default) — submitter → executor; carries a `kind:"task"` envelope per published command.
- `agent-backward-285` (default) — executor → submitter; carries a `kind:"result"` envelope per finalized task.

Names are configurable via `Settings.ntfy_forward_topic` / `Settings.ntfy_backward_topic`; server URL via `Settings.ntfy_server_url`.

### Envelopes

Three `kind`s on the backward topic — the submitter filters by `kind="result"` specifically, so ACK and result envelopes with the same `task_id` do not confuse the wait path.

Task envelope (published to the forward topic):

```json
{
  "kind": "task",
  "version": "v0.3",
  "task_id": "<utc-compact>-<sha1-8><rand-16>",
  "created_at": "<iso>",
  "submitter_id": "host:pid",
  "command": "...",
  "timeout_seconds": 300,
  "metadata": {}
}
```

**Unified transport (v0.3):** the envelope carries **one plain command string**. No `submit_mode`, no `target_host`. Every submitter CLI (`submit_gitbash.py`, `submit_gitbash_ssh.py`, `submit_powershell*.py`, …) renders its own wrapping **client-side** and submits the finished command. The executor is agnostic — it only sees `task["command"]` and runs it via `/bin/sh -c`. Optional `metadata` (e.g. `{"ssh_host": "H20"}`) is for audit / display only; the executor ignores it.

ACK envelope (published to the backward topic by the executor **before** it spawns the worker subprocess):

```json
{
  "kind": "ack",
  "version": "v0.2.1",
  "task_id": "...",
  "executor_id": "host:pid",
  "ack_at": "<iso>"
}
```

The ACK exists only for crash-recovery dedup on executor restart. See "Dedup model" below.

Result envelope (published to the backward topic):

```json
{
  "kind": "result",
  "version": "v0.2",
  "task_id": "...",
  "executor_id": "host:pid",
  "status": "done|failed|stale",
  "started_at": "...", "finished_at": "...",
  "exit_code": 0,
  "stdout_tail": "...≤4KB...",
  "stderr_tail": "...≤4KB...",
  "command_digest": "sha256:...",
  "process_ref": "pid:12345",
  "stale_at": "<iso or null>"
}
```

`task_id` is the sole dedup key. `timeout_seconds` is authoritative from the submitter; the executor rejects any envelope missing or with a non-positive value and publishes a `failed` result back.

### Runtime components

- `agent_exec_tunnel/ntfy_transport.py`
  - `publish(cfg, topic, envelope)` — bounded POST with `publish_max_attempts` retries and `Retry-After` backoff; raises `NtfyPublishError` on final failure. Used by the submitter.
  - `publish_forever(cfg, topic, envelope, ...)` — infinite retry with exponential backoff capped at 30s, honoring `Retry-After`. Each attempt builds a fresh `Request` with `Connection: close` so no pooled TCP/TLS session can rot across long outages. Used by the executor's worker threads for result publication.
  - `poll_since(cfg, topic, since)` — one-shot `GET /{topic}/json?poll=1&since={since}`, returns the list of parsed envelopes.
  - `poll_loop(cfg, topic, on_envelope, is_seen, cap_seconds, ...)` — polls the topic forever, 1s base with upward jitter capped at `cap_seconds`; dispatches new envelopes synchronously via `on_envelope`. Dedup is via the caller-supplied `is_seen(task_id) -> bool` callback — the caller is responsible for any locking around its dedup state.
  - `wait_for(cfg, topic, task_id, deadline_monotonic, cap_seconds)` — submitter-side variant that exits the moment an envelope matching `task_id` arrives.
  - `seed_seen_ids(cfg, topic)` — one-time prime of the dedup set from the 2h replay window (used by the executor on startup).
- `agent_exec_tunnel/submitter.py` — `publish_task` / `wait_for_result` / `submit_task` + `ntfy_config` helper.
- `agent_exec_tunnel/executor.py` — `Executor` class. Main thread runs `run_loop`; worker threads run `_run_task_worker` and call `_finalize_result → _publish_result → publish_forever`.
- `agent_exec_tunnel/protocol.py` — `TaskRecord` / `ResultRecord` dataclasses with `to_envelope()`; `new_task_id()` (64-bit jitter).
- `agent_exec_tunnel/config.py` — `Settings` dataclass including all `ntfy_*` knobs plus `submit_timeout_grace_seconds` (see below).
- `submitter/_submit_common.py` — shell wrappers + `submit_and_wait(label, command, mode, timeout)` used by the four `submit_{gitbash,powershell}[_ssh].py` CLIs.
- `submitter/submit_files.py` — file upload: `git_sync → copy → git_commit_push` on `agent_forward`. Only code path that still touches git.
- `tools/bootstrap_repos.py` — clone / sync `agent_forward` for hosts that push files.

### Executor concurrency model

One executor process:

1. **Main thread** enters `poll_loop`. Each iteration does one `poll_since` GET (non-blocking in practice since `poll=1` returns immediately). For each envelope whose `task_id` passes `is_seen` and the expiry guard, main thread calls `_handle_task_envelope` which claims the `task_id` into `running_tasks` (under lock), validates `timeout_seconds` / `command`, then `threading.Thread(target=_run_task_worker).start()` and returns.
2. **Worker thread** publishes an **ACK envelope** to the backward topic via `publish_forever` (blocks until accepted), then runs `subprocess.Popen(shell=True, ...)` and polls the child via `process.poll()` plus a `deadline_at` check. On child exit or deadline it calls `_finalize_result`.
3. `_finalize_result` calls `_publish_result`, which wraps `publish_forever` — the **worker** thread blocks here until the backward POST succeeds (or the executor is stopped). Only after publish success does the worker mark `task_id` into `seen_ids` and clear `running_tasks`.

Consequence: an ntfy backward-topic outage blocks just the affected workers, not the polling main thread. New envelopes can keep being dispatched to new worker threads concurrently. Because ACK is published before the subprocess is spawned, a mid-task executor crash that precedes a result publish still leaves a visible ACK; a restart within the 30-minute window seeds `seen_ids` with that ACK's task_id and skips re-execution.

### Dedup model

- `seen_ids`: `dict[task_id, monotonic_insert_time]`. A task_id lands here when **either** an ACK **or** a result envelope has been successfully published. Pruned lazily by `_maybe_prune_seen_ids` (called from `_is_seen`, bounded to run at most once per minute) so entries older than `Settings.seen_ids_ttl_seconds` (default 1h = 2× the poll window) are dropped.
- `running_tasks`: task_ids with a live worker thread (publish not yet completed).
- `_is_seen(task_id) = task_id in seen_ids or task_id in running_tasks`.
- `poll_loop` queries `is_seen` via a callback under the executor's `_state_lock`. The callback is intentionally a callable, not a raw set, so the poll thread and worker threads cannot race on an unlocked set.
- On startup, `seed_seen_ids(backward_topic)` pulls every envelope (ACK or result, any `kind`) visible in the 30-minute replay window and pre-fills `seen_ids`. This closes the **crash-mid-task** re-run window: if the previous executor instance crashed after publishing an ACK but before publishing a result, the restarted executor sees the ACK and will not re-dispatch that task_id.

### Boundary guard (expired envelopes)

Separate from dedup, there is a **per-envelope expiry check**. When the poll thread sees a task envelope whose `created_at + timeout_seconds` is already in the past, `_handle_task_envelope` marks the task_id as seen and returns without dispatching. This covers the near-window-edge race where a task envelope lingers in the 30-minute replay window but the backward topic happens to carry neither an ACK nor a result for it (often because ntfy was rate-limited or briefly partitioned when the original execution was attempted). Running a long-past-deadline task is useless (the submitter has already given up) and slightly dangerous, so the executor simply skips it.

### Polling cadence

The executor's idle cadence is `base_seconds + random.uniform(0, jitter_max)`.

- `base_seconds = 1.0` (`Settings.ntfy_poll_base_seconds`).
- `jitter_max` grows on each idle poll: `min(cap_jitter, jitter_max * 1.10 + 0.05)`. It is **reset to 0** the instant any new envelope arrives.
- `cap_jitter = default_timeout_seconds / 2 - base_seconds` (150s − 1s = 149s by default).
- On a poll error (HTTP / TCP / DNS), jitter grows just like on idle — so a flaky ntfy does not turn into a self-DoS retry loop.
- With `growth=1.10`, `cap=149s` is reached in roughly 50 consecutive empty polls (several minutes of wall clock). Over an hour of idle, most polls happen near the cap.

### Timeout semantics

- The envelope's `timeout_seconds` is the executor-side deadline for the subprocess.
- The submitter's wait deadline is `timeout_seconds + submit_timeout_grace_seconds` (default +15s). The grace absorbs publish → dispatch → worker-spawn skew so the submitter can still observe an executor-authored `stale` envelope when a task times out, instead of racing past it.
- Final-state failure paths on the submitter: `TimeoutError` with one of two messages — "ntfy reachable; executor may be down or overloaded" or "last ntfy poll failed — server may be unreachable". These distinguish "we saw fresh polls succeed but no result" from "we couldn't even reach ntfy".

## File plane (git)

`submitter/submit_files.py` copies a local path into `agent_forward/files/<namespace>/…` and runs `git fetch / rebase / push` on the forward repo. This is the only remaining git write path. Consequence: concurrent `submit_files.py` calls still serialize through the `main` branch's rebase-push loop, but the previous source of contention (task/ACK/result JSON writes) is gone, so the realistic conflict rate drops to the rate of file uploads — small.

Executor-only hosts (the common deployment) do **not** need the `agent_forward` clone. `executor/run_executor.py` emits a preflight warning when `agent_forward/.git` is missing but does not exit — the message plane is already up.

## Known MVP trade-offs (deferred)

- **ACK window** (v0.2.1): ACK is published before the subprocess is spawned, which narrows but does not close the re-run window. If the executor crashes between claiming into `running_tasks` (main thread) and the ACK POST being accepted (worker thread), a restart will re-dispatch the task. In normal operation this window is sub-second.
- **No auth**: topic names are world-readable / world-writable. Anyone who guesses `agent-forward-285` can post a task envelope; the executor will run it via `subprocess.Popen(shell=True, ...)` with full host environment. Add HMAC signing or a private ntfy instance for production.
- **No streaming**: stdout/stderr are published as 4KB tails at completion. A large-log task should arrange out-of-band transport (log to file, then upload via `submit_files.py`).
- **`shell=True`**: inherits the full process environment, cwd, and SSH config to the task subprocess. Intentional (it's a tunnel), but pairs with the auth gap above to form a documented trust boundary.
- **TLS trust store**: `ntfy_transport` soft-imports `truststore` and `inject_into_ssl()` so system-CA chains are honored. If `truststore` is not installed, urllib's default (Python-bundled CAs) is used — typically fine but may miss corporate or custom CAs.

## Availability monitoring

`tests/availability/` drives the real submitter CLIs on a randomized schedule (burst + quiet-tick Bernoulli) and writes records into JSONL. `tests/availability/report.py` renders an HTML dashboard with hop cards (by `implies_ok` tag), p50/p95/p99 latency, preview + total stage timings, a 24h hourly SVG heartbeat, per-probe table, and recent failures. See `tests/availability/README.md` for operation and `tests/availability/ssh` for the `local_relay`-mode ssh shim.

## Transport flow (v0.3)

The quoting history: chaining `user bash → executor sh -c → ssh argv join → remote shell -c → command shell` produces up to 5 shell-parse layers. Every layer chews one `\`. For any non-trivial payload this balloons into `\\\\\\\"`-level escaping that no human can maintain. We solve it by **base64-encoding the user's payload at the submitter**, wrapping it in an inert `bash -c "$(echo '<b64>' | base64 -d)"` trampoline, and letting every intermediate shell see the base64 blob as one atomic literal — the payload bytes **are not parsed by any shell** except the final one that runs them.

### End-to-end for `submit_gitbash_ssh.py H20 '<payload>'`

```
USER                        SUBMITTER (client)                EXECUTOR (sh -c)          SSH client            REMOTE BASH
──────                      ──────────────────                ────────────────          ──────────            ───────────
payload bytes          ──▶  base64(payload) = <B64>       ──▶ sh parses:            ──▶ stdin: argv[2..]  ──▶ $SHELL -c "<received>"
(single-quote outer;        relay =                           ssh LOCAL              ─▶ pure byte join,     parses dquoted $() :
 bash passes through)       ssh HOST                          "bash -c …$()…"        no re-quoting         runs subshell
                              "bash -c                        (0 payload chars                             echo '<B64>' | base64 -d
                                 \"\$(echo '<B64>'             consumed — <B64>                            → decoded = <payload>
                                  | base64 -d)\""              lives in '…')                               bash -c <payload>
                            envelope:                                                                      ↓
                              { "kind":"task",                                                             FINAL SHELL PARSE
                                "command": relay,                                                          (the only layer that
                                ... }                                                                      reads user bytes)
                            publish to ntfy forward                                                        ↓
                                                                                                           exec <user command>
```

**Parse-count table** (layers that read the payload's bytes):

| Layer | Sees payload? | Consumes quotes? |
|---|---|---|
| User's outer bash | ✓ (but single-quote shields it) | 0 |
| Submitter encoder (base64) | — (pure byte transform) | 0 |
| JSON + ntfy over the wire | — (opaque) | 0 |
| Executor `sh -c <relay>` | payload lives inside `'<B64>'` literal | 0 |
| SSH client argv join | — (byte concat) | 0 |
| Remote `$SHELL -c` parsing `bash -c "$(…)"` | `$()` decodes; result not re-parsed | 0 |
| Remote `bash -c <decoded>` | **this one parses `<payload>` as shell source** | **1** |

Total: **1 shell parse of the payload**, which is exactly what the user expects when they write shell code.

### Preview output is for humans

The submitter CLIs still print the legacy three-line human-readable preview:

```
-> "C:\Program Files\Git\bin\bash.exe" -c "ssh H20 'python3 -c ...'"
  -> ssh H20 'python3 -c "print(\"hello\nworld\")"'
    -> python3 -c "print(\"hello\nworld\")"
```

**This preview does not represent what the executor actually runs** — the real command is the base64-wrapped form. The preview exists for operator comprehension: reading `ssh HOST '<payload>'` makes the intent obvious. Inspect the actual wire command from `agent_exec_tunnel.submitter._submit_common.render_gitbash_ssh_command(host, payload)` if a bug manifests downstream.

### What the unified envelope means for other tools

Because there is only one command field on the wire, any future submitter (`submit_kubectl.py`, `submit_docker_exec.py`, `submit_from_stdin.py`, …) plugs in the same way: render a plain `str` client-side, call `submit_and_wait(label, command, timeout, metadata=…)`. The executor is never aware of the flavor.