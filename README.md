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
- `python3 tools/run_burst_real_submodules.py --duration-seconds 30 --tasks 30 --use-fake-ssh`
- `python3 tools/repair_task.py --task-id ... --clear-ack`
- `python3 tests/availability/probe.py --probe-id relay_echo --count 1`
- `python3 tests/availability/probe.py --probe-id ssh_h20_nvidia_smi --ssh-host H20 --count 1`
- `python3 tests/availability/report.py --serve`

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

## Real Submodule Burst

If you want a pressure run whose task / ACK / result files are directly visible under the checked-out submodules, use:

```bash
python3 tools/run_burst_real_submodules.py --duration-seconds 30 --tasks 30 --use-fake-ssh
```

Behavior:

- executor uses the checked-out `agent_forward/` and `agent_backward/` submodule working trees
- submitters use temporary clones against the same remotes so multiple concurrent submits are possible
- after the run, the submodule working trees are synced again so you can inspect the resulting files locally

`python3 tools/bootstrap_repos.py` now also repairs local file-based submodule origins: if a submodule still points at an out-of-repo sibling path, bootstrap rewires it to a repository-local bare remote under `var/local_remotes/`.

The checked-in `.gitmodules` now uses explicit HTTPS URLs:

- `https://github.com/hyjack-00/agent_forward.git`
- `https://github.com/hyjack-00/agent_backward.git`

## Synchronization

Even though forward and backward are single-purpose data repositories, synchronization is still part of correctness:

- submitter must sync forward and backward before publication
- submitter must sync backward before trusting final result visibility
- executor must sync forward and backward before deciding whether a task is claimable
- backward is the only authority for terminal state
- the continuously-running executor now retries transient git sync/push failures forever with backoff
- task subprocess timeout is written as a durable `failed` result instead of crashing the executor

Relay and ssh are different submit wrappers, but they are the same runtime class of work for executor: one claimed task becomes one executed command.

The checked-out submodule working directories used by the default CLI settings are:

- `agent_forward/`
- `agent_backward/`

## Working Clone Rule

Running submitter and executor against the same remotes is supported.

Running them against the same working clone is not the supported deployment model.

Use separate working clones even on one machine:

- one submitter clone for forward/backward access on the caller side
- one executor clone for forward/backward access on the runner side

This repository's local integration coverage uses that exact separation.

If you launch both `submitter/*.py` and `executor/run_executor.py` directly against the checked-out `agent_forward/` and `agent_backward/` submodules in this same repo, they will share one git working tree per data repo. That can race because both sides call sync operations that do `fetch + checkout/reset`, and both sides also create commits. So:

- same remotes: supported
- same checked-out submodule working tree: not supported for concurrent submit + execute

For local same-machine diagnostics without that conflict, keep executor on the checked-out submodules and give submitters their own temporary clones against the same submodule remotes, as `tools/run_burst_real_submodules.py` already does.

The important distinction is:

- conflict is **not** "submitter and executor edit the same JSON file"
- conflict **is** "two processes operate on the same git working tree and index"

## Long-Running Executor

The supported executor model is a long-running loop:

- transient fetch/push failures must be retried forever
- temporary disconnects are expected to recover later
- executor must keep reconnecting instead of exiting
- task timeout must become one durable final result in `agent_backward/results/...`

More specifically, the current executor behavior is:

- scan finds one claimable task
- ACK is pushed durably first
- only after durable ACK does executor run the task
- executor then blocks on that task until exit / timeout
- final output is published once as one final result record
- submitter polls only for final result, not for ACK or streaming output

## Skill

The repository-local Codex skill for the new architecture is:

- [.codex/skills/agent-exec-tunnel-submit/SKILL.md](.codex/skills/agent-exec-tunnel-submit/SKILL.md)
