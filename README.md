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

The data repos `agent_forward/` and `agent_backward/` are **not** submodules. They are plain sibling clones sitting inside the tunnel checkout and listed in `.gitignore`. They are provisioned by `tools/bootstrap_repos.py`.

Fresh-machine first run — two steps, in order:

```bash
git clone <AgentExecTunnel remote> && cd AgentExecTunnel
python3 tools/bootstrap_repos.py                 # 1. clone agent_forward and agent_backward
python3 executor/run_executor.py                 # 2. start executor
```

Prerequisites: `python3` + `git` (code is stdlib-only, no `pip install` needed). Bootstrap clones from the defaults in `agent_exec_tunnel/remotes.py`:

- `https://github.com/hyjack-00/agent_forward.git`
- `https://github.com/hyjack-00/agent_backward.git`

To point at different remotes (private fork, local bare repo for testing, etc.) set env vars or pass CLI flags:

```bash
AET_FORWARD_REMOTE=... AET_BACKWARD_REMOTE=... python3 tools/bootstrap_repos.py
# or
python3 tools/bootstrap_repos.py --forward-url ... --backward-url ... --branch main
```

An optional `.aet-remotes.json` at the tunnel root is read before falling back to defaults (also gitignored).

### What can be skipped on subsequent updates

After the first successful bootstrap, routine update on the same machine is just:

```bash
git pull                                          # tunnel-only; does not touch agent_forward/backward
python3 executor/run_executor.py
```

- Skip `bootstrap_repos.py` unless: data-repo remote URL changed, the data dirs got wiped, or you moved the checkout. Bootstrap is idempotent (re-sync via `fetch + reset --hard`), so re-running it is safe.
- Skip Python dependency setup entirely — there is none.
- **Never** run `git submodule ...` against this repo; it no longer uses submodules.

Always needed, even on updates:

- A running executor loop (`executor/run_executor.py`) — it does not survive `git pull` by itself; restart after pulling.
- Network access to the data-repo remotes — the executor does `git fetch` every scan pass.

### Why not submodules

Submodule pinning is wrong for these two repos: they mutate on **every** task (new commits for task publication, ACK, result), so the tunnel commit would constantly need to bump its submodule SHA, and any runtime commit that was not pushed to the configured remote would produce `not our ref` errors on a new machine. Treating them as ignored sibling clones removes the whole class of ghost-SHA failures.

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
- all command / ACK / result traffic still goes through the `agent_forward` / `agent_backward` remotes resolved from `agent_exec_tunnel/remotes.py` (env vars / `.aet-remotes.json` / defaults)
- after the run, the local `agent_forward/` and `agent_backward/` clones in this workspace are re-synced to those remotes so you can inspect the latest visible state locally

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
- traffic still goes through the live `agent_forward` / `agent_backward` remotes resolved from `agent_exec_tunnel/remotes.py`
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

The repo-local data directories used by the default CLI settings are:

- `agent_forward/` (gitignored, cloned by bootstrap)
- `agent_backward/` (gitignored, cloned by bootstrap)

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

If you launch both `submitter/*.py` and `executor/run_executor.py` directly against the repo-local `agent_forward/` and `agent_backward/` clones in this same repo, they will share one git working tree per data repo. That can race because both sides call sync operations that do `fetch + checkout/reset`, and both sides also create commits. So:

- same remotes: supported
- same repo-local data-repo working tree: not supported for concurrent submit + execute
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
