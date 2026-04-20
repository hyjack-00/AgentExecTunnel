# Availability

Availability monitoring for AgentExecTunnel.

## Probe presets

Defined in `probes.py`: `relay_echo`, `ssh_echo`, `ssh_h20_echo`, `ssh_h20_hostname`,
`ssh_h20_nvidia_smi`, `ssh_950_echo`, `ssh_910_echo`.

## Usage

```bash
# single probe
python3 tests/availability/probe.py --probe-id relay_echo --count 1

# continuous with bursty traffic (mean 30s between requests, burst peak 1 rps)
python3 tests/availability/probe.py --count -1 --mean-period 30 --burst-peak-rps 1

# rotate all probes, infinite mode
python3 tests/availability/probe.py --count -1

# specific host override
python3 tests/availability/probe.py --probe-id ssh_950_echo --count -1 --ssh-host 950

# serve report
python3 tests/availability/report.py --serve
```

## Traffic shape

When `--count=-1`, the probe runs indefinitely (Ctrl-C / SIGTERM to stop).

The traffic pattern uses a Bernoulli burst model:
- **Mean period** (`--mean-period`, default 30s): long-run average interval between requests
- **Burst peak RPS** (`--burst-peak-rps`, default 1): request rate during burst windows
- **Burst duration** (`--burst-duration-min` / `--burst-duration-max`): random burst window length

Data files older than `--retention-days` (default 1) are automatically pruned.
