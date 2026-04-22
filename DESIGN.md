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
- `submitter/submit_files.py` — synchronous verified file upload: `git_sync → namespace-unique check → copy → git_commit_push (3×15s retry) → ntfy-dispatch remote pull+verify task → block on result`. Renders the remote command client-side so the executor stays mode-agnostic.
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

- `base_seconds = 5.0` (`Settings.ntfy_poll_base_seconds`, v0.4.2).
- `jitter_max` grows on each idle poll: `min(cap_jitter, jitter_max * 1.5 + 1.0)`. It is **reset to 0** the instant any new envelope arrives.
- `cap_jitter = ntfy_poll_cap_seconds - base_seconds` (295s by default at v0.4.2). The cap is a standalone setting rather than derived from `default_timeout_seconds`, because idle cadence is about broker politeness + reducing the periodic-beacon signature, not task timeout.
- On a poll error (HTTP / TCP / DNS), jitter grows just like on idle — so a flaky ntfy does not turn into a self-DoS retry loop.
- At saturation each poll is an approximately uniform draw over `[5, 300]s` (mean ≈ 152s, ≈23 GETs/hour). This wider band — up from v0.4.1's `[3, 150]s` — is a direct mitigation for corporate gateways that auto-isolate new domains based on a combination of daily request volume, regularity (self-correlation in the time-series), and client-diversity heuristics.

### Timeout semantics

- The envelope's `timeout_seconds` is the executor-side deadline for the subprocess.
- The submitter's wait deadline is `timeout_seconds + submit_timeout_grace_seconds` (default +15s). The grace absorbs publish → dispatch → worker-spawn skew so the submitter can still observe an executor-authored `stale` envelope when a task times out, instead of racing past it.
- Final-state failure paths on the submitter: `TimeoutError` with one of two messages — "ntfy reachable; executor may be down or overloaded" or "last ntfy poll failed — server may be unreachable". These distinguish "we saw fresh polls succeed but no result" from "we couldn't even reach ntfy".

## File plane (git + ntfy verification)

`submitter/submit_files.py` is a **synchronous, verified** file transfer: it blocks until the executor has pulled the file AND confirmed the file is present in its local tree. It is a composition of the two existing planes — GitHub for bytes, ntfy for the pull-and-check round-trip — not a new transport.

**Flow** (v0.4):

1. **Local pre-sync** (best-effort `git fetch + reset --hard origin/main` on the submitter's forward repo) so the subsequent namespace-uniqueness check reflects the true remote state. Failure here is a warning, not fatal.
2. **Namespace uniqueness**: reject if `agent_forward/files/<name>/` already exists locally. Namespaces are one-shot — a given `--name` may be used only once per forward repo.
3. **Stage**: `copy_tree_or_file(src, agent_forward/files/<name>/<src.name>)`.
4. **Push with bounded retry**: up to 3 attempts with 15 s between retries. Each attempt runs `git_commit_push` (which has its own small-cap rebase loop for concurrent-push collisions). Total wall budget ≤ ~45 s + network time. Final failure exits 1 with a `please re-run` hint.
5. **Render remote verify command**: `submitter/submit_files.py::_render_remote_verify_command(name, filename)` produces a single-string bash command that:
   - resolves `$AET_FORWARD_ROOT` (fallback: `agent_forward` relative to the executor's cwd)
   - retries `git fetch + reset --hard origin/main` up to 3 times with 15 s between attempts
   - runs `[ -e files/<name>/<filename> ]` and prints `VERIFY_OK …` (exit 0) or `VERIFY_MISSING …` (exit 12)
   - distinct exit codes: 10 forward_root missing, 11 pull kept failing, 12 file absent after a successful pull

   The command is rendered **entirely client-side**. The executor is still mode-agnostic — it just runs `bash -c <command>` via the usual ntfy task path. There is no special-case handling of file uploads in `Executor`.
6. **Ntfy dispatch + block**: `publish_task(command=verify_cmd, timeout_seconds=120)` then `wait_for_result`. The 120 s budget covers `3 × 15 s` retry sleeps plus actual pull time.
7. **Interpret result**:
   - exit 0 → `VERIFIED namespace=<name>`, process exits 0.
   - exit 12 or stderr contains `VERIFY_MISSING` → upload + pull OK but file absent; exit 3 with a retry hint.
   - anything else (exit 10/11, stale envelope, ntfy timeout) → exit 2 and print the exact manual bash command so the operator can retry on the executor host.

**Rendering choice**. The remote command lives in `submit_files.py` — not in `executor.py` — so the same executor binary runs regular tasks and verify tasks through one code path. Changing the verify semantics is a submitter-side edit; no executor redeploy.

**Concurrency note**. Concurrent `submit_files.py` calls still serialize at the git `main` branch's push level. The internal rebase loop (`git_commit_push(max_attempts=8)`) is bounded; the outer `3 × 15 s` retry loop absorbs transient collisions. This is acceptable for the single-submitter workflow that the message plane already assumes.

Executor-only hosts (the common deployment) do **not** need the `agent_forward` clone. `executor/run_executor.py` emits a preflight warning when `agent_forward/.git` is missing but does not exit — the message plane is already up. Hosts that want to receive file uploads must have `agent_forward` bootstrapped and (optionally) `AET_FORWARD_ROOT` exported if the executor's cwd is not the repo root.

## Known MVP trade-offs (deferred)

- **ACK window** (v0.2.1): ACK is published before the subprocess is spawned, which narrows but does not close the re-run window. If the executor crashes between claiming into `running_tasks` (main thread) and the ACK POST being accepted (worker thread), a restart will re-dispatch the task. In normal operation this window is sub-second.
- **Ntfy auth** (partially closed in v0.3.3): the `ntfy_transport.NTFY_AUTH_TOKEN` constant (or `AET_NTFY_TOKEN` env var) attaches `Authorization: Bearer …` to every publish / poll / attachment-fetch. Against a private ntfy or ntfy Pro this gates access end-to-end. Against public `ntfy.sh` the topic is still world-readable / world-writable — an access token only authenticates the ACCOUNT, not the TOPIC. For tamper-proof transport you still need HMAC signing on the envelope or a truly private ntfy instance with ACL'd topics.
- **Submitter hardening** (v0.3.4): ssh host argument is validated (`[A-Za-z0-9._@:-]+`, no leading `-`) to block ssh-option injection; every renderer pre-flights the payload against a 100 KB ARG_MAX limit with a pointer to `submit_files.py`; the remote base64 trampoline (both bash and PowerShell ssh paths) checks `command -v base64` (exits 127 when missing) and the decoded output length (exits 97 on empty) before `exec`-ing the payload. These close concrete failure modes rather than a category — injection-proof multi-tenant transport still requires per-topic ACLs and envelope HMAC.
- **Executor-side wire budget + bounded publish retry** (v0.4.1): relay hosts running the executor behind a VPN audit enforce a per-HTTP-packet upper bound (~80–100 KB) that silently drops oversized bodies. Two countermeasures: (1) before publishing an ACK or result envelope, the executor truncates stdout/stderr tails so the JSON-encoded body fits under `Settings.ntfy_result_wire_budget_bytes` (default 60 000; override via `AET_NTFY_RESULT_WIRE_BUDGET_BYTES`), prefixing a `[truncated by executor: original NB, envelope wire budget NB]` marker and preserving the *tail of the tail* (most diagnostic bytes). (2) `publish_forever` accepts a `deadline_monotonic` parameter; the executor passes `now + task.timeout_seconds` so a wedged backward publish cannot outlive the task's own timeout budget — if the deadline hits before ntfy accepts the envelope, the worker thread releases and the submitter surfaces "ntfy reachable; executor may be down or overloaded". HTTP-level distinction between "audit rejected" and "ordinary network flake" is not attempted; both present identically at the urlopen layer, so the size guard and the deadline together form the pragmatic fallback.
- **Anti-beacon poll tuning + Gitee file-plane default** (v0.4.2): deployed behind a Chinese corporate gateway (netentsec family) the tunnel was observed to hit a "new-domain auto-isolation" pattern — ntfy.sh went dark for several hours at a time, then auto-recovered. Diagnostic ruled out SNI blacklist and rate limiting; the gateway's trigger combines daily request volume, time-series regularity, and single-client novelty. Two countermeasures: (1) `ntfy_poll_base_seconds 3→5`, `ntfy_poll_jitter_floor 0.05→1.0`, and a new standalone `ntfy_poll_cap_seconds=300` decoupled from `default_timeout_seconds`. Idle saturation now draws from `[5, 300]s` (mean ≈ 152s vs. previous ≈ 76s), roughly halving GET volume and doubling the interval band's width — small reduction in periodicity self-correlation, not a cure. (2) `DEFAULT_FORWARD_REMOTE` switched from `github.com/hyjack-00/agent_forward.git` to `gitee.com/hyjack-00/agent_forward.git`: in the target deployment region, Gitee is an order of magnitude faster for both push and pull, and as a domestic origin it is far less likely to enter "new-domain" isolation in enterprise gateways. Message plane remains on ntfy.sh for now. **Not a cure for the underlying heuristic** — polling changes at best delay re-trigger; root fixes (IT whitelist, self-hosted pub/sub on an already-trusted domain, moving the message plane to Gitee too) are tracked in `PLAN.md` §9 and deferred pending observation data.
- **No streaming**: stdout/stderr are published as 4KB tails at completion. A large-log task should arrange out-of-band transport (log to file, then upload via `submit_files.py`).
- **`shell=True`**: inherits the full process environment, cwd, and SSH config to the task subprocess. Intentional (it's a tunnel), but pairs with the auth gap above to form a documented trust boundary.
- **TLS trust store**: `ntfy_transport` soft-imports `truststore` and `inject_into_ssl()` so system-CA chains are honored. If `truststore` is not installed, urllib's default (Python-bundled CAs) is used — typically fine but may miss corporate or custom CAs.

## Availability monitoring

`tests/availability/` drives the real submitter CLIs on a randomized schedule (burst + quiet-tick Bernoulli) and writes records into JSONL. `tests/availability/report.py` renders an HTML dashboard with hop cards (by `implies_ok` tag), p50/p95/p99 latency, preview + total stage timings, a 24h hourly SVG heartbeat, per-probe table, and recent failures. See `tests/availability/README.md` for operation and `tests/availability/ssh` for the `local_relay`-mode ssh shim.

## Transport flow (v0.3.2)

Two orthogonal improvements over v0.3.0:

1. **No more `Popen(str, shell=True)`**. Python's `shell=True` hardcodes `/bin/sh -c` on Linux and `cmd.exe /c` on Windows — an unavoidable extra shell layer. As of v0.3.2 the executor uses `Popen([executor_shell, *executor_shell_args, task["command"]], shell=False)` with `executor_shell` picked from config / env (`/bin/bash` on Linux, Git Bash on Windows by default, override via `AET_EXECUTOR_SHELL`). Net: payload passes through **one** shell parse (the configured shell), not two.
2. **Envelope stays a single `str`**. Submitter CLIs are just convenience — they render specific payload shapes (ssh base64 trampoline, `powershell.exe -EncodedCommand`, raw pass-through, …) client-side. The executor never branches on CLI flavor, it just runs the string. A new `submitter/submit.py` skips *all* rendering so complex payloads can be hand-crafted when the helpers aren't flexible enough.

### End-to-end for `submit_gitbash_ssh.py H20 '<payload>'`

The quoting history: chaining `user bash → executor sh -c → ssh argv join → remote shell -c → command shell` produces up to 5 shell-parse layers. Every layer chews one `\`. For any non-trivial payload this balloons into `\\\\\\\"`-level escaping that no human can maintain. We solve it by **base64-encoding the user's payload at the submitter**, wrapping it in an inert `bash -c "$(echo '<b64>' | base64 -d)"` trampoline, and letting every intermediate shell see the base64 blob as one atomic literal — the payload bytes **are not parsed by any shell** except the final one that runs them.

### End-to-end for `submit_gitbash_ssh.py H20 '<payload>'`

```
USER                   SUBMITTER (client)                EXECUTOR                  SSH client            REMOTE BASH
──────                 ──────────────────                ────────                  ──────────            ───────────
payload bytes     ──▶  base64(payload) = <B64>       ──▶ Popen([bash.exe,      ──▶ stdin: argv[2..]  ──▶ $SHELL -c "<received>"
(single-quote          relay =                           "-c", relay],             pure byte join,     parses dquoted $() :
 outer; bash             ssh HOST                        shell=False)              no re-quoting         runs subshell
 passes through)         "bash -c                        └── bash parses                                 echo '<B64>' | base64 -d
                            \"\$(echo '<B64>'               relay: calls ssh                             → decoded = <payload>
                             | base64 -d)\""                with argv[2] =                               bash -c <payload>
                       envelope:                            "bash -c $(echo                              ↓
                         { "kind":"task",                   '<B64>' | base64                             FINAL SHELL PARSE
                           "command": relay,                -d)"                                         (the only layer that
                           ... }                                                                         reads user bytes)
                       publish to ntfy forward                                                           ↓
                                                                                                         exec <user command>
```

**Parse-count table** (layers that read the payload's bytes):

| Layer | Sees payload? | Consumes quotes? |
|---|---|---|
| User's outer bash | ✓ (but single-quote shields it) | 0 |
| Submitter encoder (base64) | — (pure byte transform) | 0 |
| JSON + ntfy over the wire | — (opaque) | 0 |
| Executor `Popen([bash, -c, relay], shell=False)` | payload lives inside `'<B64>'` literal | 0 |
| SSH client argv join | — (byte concat) | 0 |
| Remote `$SHELL -c` parsing `bash -c "$(…)"` | `$()` decodes; result not re-parsed | 0 |
| Remote `bash -c <decoded>` | **this one parses `<payload>` as shell source** | **1** |

Total: **1 shell parse of the payload**, which is exactly what the user expects when they write shell code. Compared to v0.3.1 this saves one layer (the executor side Python-hardcoded `sh -c` / `cmd.exe /c` is gone).

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