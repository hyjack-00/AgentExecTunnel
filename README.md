# AgentExecTunnel

Public control repository for the dual-repo execution tunnel.

The runtime layout is repository-local:

- `AgentExecTunnel`
- `AgentExecTunnel/agent_forward`
- `AgentExecTunnel/agent_backward`

Protocol roles:

- submitter writes `agent_forward`, reads `agent_backward`
- executor reads `agent_forward`, writes `agent_backward`

Authoritative task state is always in `agent_backward`.

The architecture allows multiple submitters to publish into forward concurrently. Forward publication therefore must converge through git fetch/rebase/push retry rather than a single-submit assumption.

## Repositories

- `agent_forward/tasks/YYYY/MM/DD/HH/<task_id>.json`
- `agent_forward/files/<user_name>/...`
- `agent_backward/acks/YYYY/MM/DD/HH/<task_id>.json`
- `agent_backward/results/YYYY/MM/DD/HH/<task_id>.json`

## Main tools

- `python3 tools/bootstrap_repos.py`
- `python3 submitter/submit_powershell.py 'echo hello'`
- `python3 submitter/submit_powershell_ssh.py H20 'uname -a'`
- `python3 submitter/submit_gitbash.py 'ls /c/Users/'`
- `python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'`
- `python3 submitter/submit_files.py --name demo --src /path/to/file-or-dir`
- `python3 executor/run_executor.py`
- `python3 tools/run_burst_local_relay.py --tasks 30 --interval-seconds 1 --submit-timeout 512 --result-timeout 900 --executor-ready-timeout 120 --submitter gitbash --gitbash-executable /path/to/bash`
- `python3 tools/run_burst_live.py --duration-seconds 30 --tasks 30 --submit-timeout 512 --result-timeout 300 --mode-set mixed --submitter gitbash --gitbash-executable /path/to/bash`
- `python3 tools/repair_task.py --task-id ... --clear-ack`
- `python3 tests/availability/probe.py --probe-id relay_echo --count 1`
- `python3 tests/availability/probe.py --probe-id ssh_h20_nvidia_smi --ssh-host H20 --count 1`
- `python3 tests/availability/report.py --serve`

## Startup

Fresh-machine first run — three steps, in order:

```bash
git clone <AgentExecTunnel remote> && cd AgentExecTunnel
git submodule update --init --recursive          # 1. fetch agent_forward / agent_backward
python3 tools/bootstrap_repos.py                  # 2. verify submodule origins, repair if needed
python3 executor/run_executor.py                  # 3. start executor
```

Prerequisites: `python3` + `git` (code is stdlib-only, no `pip install` needed). The submodule remotes listed in `.gitmodules` must be reachable from this machine.

### What can be skipped on subsequent updates

After the first successful bootstrap, routine update on the same machine is just:

```bash
git pull
git submodule update --recursive                  # pick up tunnel-pinned submodule SHAs
python3 executor/run_executor.py
```

- Skip `--init`: submodules are already initialized; `--recursive` alone is enough to advance them.
- Skip `bootstrap_repos.py` **unless** one of these changed: `.gitmodules` URLs, the `var/local_remotes/` layout, or the submodule `remote.origin.url` got rewritten. Bootstrap is idempotent, so re-running it is safe but usually a no-op on steady-state machines.
- Skip Python dependency setup entirely — there is none.

Always needed, even on updates:

- A running executor loop (`executor/run_executor.py`) — it does not survive `git pull` by itself; restart after pulling.
- Network access to the submodule remotes — the executor does `git fetch` every scan pass.

### When a re-bootstrap is actually required

- First clone on a new machine.
- Switching the submodule origin between GitHub HTTPS and a local bare remote under `var/local_remotes/`.
- Recovering from a wiped `agent_forward/` or `agent_backward/` working tree.
- Moving the checkout to a new path where relative submodule origins no longer resolve.

## Process

All work is tracked in [PROGRESS.md](PROGRESS.md).

Every version requires:

- `DESIGN.md` update
- `reviews/vX.Y.Z.md`
- `evaluations/vX.Y.Z.md`
- test/evaluation run

## Availability

Availability monitoring now lives in `tests/availability/`.

- `probe.py` records probe results into `var/availability/data-YYYYMMDD.jsonl`
- `report.py` builds `var/availability/reports/report-latest.html`

The current availability data model reports:

- ACK latency
- execution latency
- result latency
- total latency

Probe presets include relay and ssh variants, and ssh probes may override the target with `--ssh-host`.

## Local Relay Burst

The supported same-machine burst diagnostic is `tools/run_burst_local_relay.py`.

Behavior:

- it creates two isolated whole-tunnel working copies under a temp dir
- one copy runs the executor
- one copy acts as the submitter-side base clone
- each submitted task gets its own submitter-side working copy
- all command / ACK / result traffic still goes through the `agent_forward` / `agent_backward` remotes from `.gitmodules`
- after the run, the checked-out submodules in this workspace are re-synced to those remotes so you can inspect the latest visible state locally

`python3 tools/bootstrap_repos.py` now also repairs local file-based submodule origins: if a submodule still points at an out-of-repo sibling path, bootstrap rewires it to a repository-local bare remote under `var/local_remotes/`.

The checked-in `.gitmodules` now uses explicit HTTPS URLs:

- `https://github.com/hyjack-00/agent_forward.git`
- `https://github.com/hyjack-00/agent_backward.git`

On non-Windows hosts, `submit_gitbash.py` can still be used by overriding the executable path:

- CLI: `--gitbash-executable /path/to/bash` on the burst tools
- environment: `AET_GIT_BASH_EXECUTABLE=/path/to/bash`

## Live Burst

The supported live submit-pressure tool is `tools/run_burst_live.py`.

Behavior:

- it assumes a remote executor is already running elsewhere
- it does not start any executor locally
- it creates one isolated submitter-side base clone under a temp dir
- each launched task gets its own submitter-side tunnel clone
- traffic still goes through the live `agent_forward` / `agent_backward` remotes from `.gitmodules`
- it measures submit/result outcomes from the caller side only

## Synchronization

Even though forward and backward are single-purpose data repositories, synchronization is still part of correctness:

- submitter must sync forward and backward before publication
- submitter must sync backward before trusting final result visibility
- executor startup may sync backward for recovery, but steady-state dispatch only syncs forward
- backward is the only authority for terminal state
- executor retries transient git sync/push failures forever with backoff
- submitter uses bounded retry on pre-publish sync and publish push paths
- task subprocess timeout is written as a durable `stale` result instead of crashing the executor

Relay and ssh are different submit wrappers, but they are the same runtime class of work for executor: one claimed task becomes one executed command.

The checked-out submodule working directories used by the default CLI settings are:

- `agent_forward/`
- `agent_backward/`

## Working Clone Rule

Running submitter and executor against the same remotes is supported.

Running them against the same working clone is not the supported deployment model.

Running more than one executor against the same remotes is also not the supported deployment model.

Use separate working clones even on one machine:

- one submitter clone for forward/backward access on the caller side
- one executor clone for forward/backward access on the runner side

Use one executor clone only:

- startup recovery imports backward state once
- steady-state duplicate suppression then relies on local in-memory task state
- the current protocol therefore assumes one active executor per remote pair

This repository's local integration coverage uses that exact separation.

If you launch both `submitter/*.py` and `executor/run_executor.py` directly against the checked-out `agent_forward/` and `agent_backward/` submodules in this same repo, they will share one git working tree per data repo. That can race because both sides call sync operations that do `fetch + checkout/reset`, and both sides also create commits. So:

- same remotes: supported
- same checked-out submodule working tree: not supported for concurrent submit + execute
- multiple executors against one remote pair: not supported

For local same-machine diagnostics without that conflict, use `tools/run_burst_local_relay.py`, which gives executor and submitter separate working copies against the same remotes.

The important distinction is:

- conflict is **not** "submitter and executor edit the same JSON file"
- conflict **is** "two processes operate on the same git working tree and index"

## Long-Running Executor

The supported executor model is a long-running loop:

- transient fetch/push failures must be retried forever
- temporary disconnects are expected to recover later
- executor must keep reconnecting instead of exiting
- task timeout must become one durable protocol result in `agent_backward/results/...`

More specifically, the current executor behavior is:

- startup does one backward recovery sync, then steady-state scans only sync forward
- scan finds one claimable task
- ACK is pushed durably first by a single git-writer thread
- only after durable ACK does executor start one async worker
- worker then owns `execute -> finalize` and the main loop does not poll child state
- timeout writes one durable `stale` result and leaves the local process detached
- final output is still published only once as one final result/stale record
- submitter polls only for final result, not for ACK or streaming output

## Skill

The repository-local Codex skill for the new architecture is:

- [.codex/skills/agent-exec-tunnel-submit/SKILL.md](.codex/skills/agent-exec-tunnel-submit/SKILL.md)
