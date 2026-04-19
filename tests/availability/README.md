# Availability

Availability monitoring stays in `AgentExecTunnel`.

This directory is reserved for the migrated availability probe/report tooling from the old monolith.

Migration is tracked in [PROGRESS.md](../../PROGRESS.md).

Current probe presets are defined in `probes.py`.

Examples:

```bash
python3 tests/availability/probe.py --probe-id relay_echo --count 1
python3 tests/availability/probe.py --probe-id ssh_h20_nvidia_smi --ssh-host H20 --count 1
python3 tests/availability/report.py --serve
```
