# AgentExecTunnel

Public control repository for the dual-repo execution tunnel.

The system has three repositories under `/workspace`:

- `AgentExecTunnel`
- `agent_forward`
- `agent_backward`

Protocol roles:

- submitter writes `agent_forward`, reads `agent_backward`
- executor reads `agent_forward`, writes `agent_backward`

Authoritative task state is always in `agent_backward`.

Current architecture version starts from `v0.0.1`.

## Repositories

- `forward/tasks/YYYY/MM/DD/HH/<task_id>.json`
- `forward/files/<user_name>/...`
- `backward/acks/YYYY/MM/DD/HH/<task_id>.json`
- `backward/results/YYYY/MM/DD/HH/<task_id>.json`

## Main tools

- `python3 tools/bootstrap_repos.py`
- `python3 submitter/submit_task.py --command 'echo hello'`
- `python3 tools/submit_files.py --name demo --src /path/to/file-or-dir`
- `python3 executor/run_executor.py`
- `python3 tools/repair_task.py --task-id ... --clear-ack`

## Process

All work is tracked in [PROGRESS.md](PROGRESS.md).

Every version requires:

- `DESIGN.md` update
- `reviews/vX.Y.Z.md`
- `evaluations/vX.Y.Z.md`
- test/evaluation run
