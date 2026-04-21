# Availability

Availability monitoring for AgentExecTunnel.

## Probe presets

Defined in `probes.py`: `relay_echo`, `ssh_echo`, `ssh_h20_echo`, `ssh_h20_hostname`,
`ssh_h20_nvidia_smi`, `ssh_950_echo`, `ssh_910_echo`.

## Usage

```bash
# single pinned probe, continuous foreground loop
python3 tests/availability/probe.py --probe-id relay_echo

# continuous with bursty traffic (mean 30s between requests, burst peak 1 rps)
python3 tests/availability/probe.py --mean-period 30 --burst-peak-rps 1

# rotate all probe presets forever
python3 tests/availability/probe.py

# override target host for SSH probes
python3 tests/availability/probe.py --probe-id ssh_950_echo --ssh-host 950

# serve report
python3 tests/availability/report.py --data-dir var/availability --serve
```

## Traffic shape

The probe always runs indefinitely in the foreground (Ctrl-C / SIGTERM to stop).
Use `--probe-id` to pin one preset; omit it to rotate across all presets.

The traffic pattern uses a Bernoulli burst model:
- **Mean period** (`--mean-period`, default 300s): long-run average interval between requests
- **Burst peak RPS** (`--burst-peak-rps`, default 5): request rate during burst windows
- **Burst duration** (`--burst-duration-min` / `--burst-duration-max`): random burst window length

Data files older than 1 day are automatically pruned.
