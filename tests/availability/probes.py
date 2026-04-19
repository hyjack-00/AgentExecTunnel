from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    submit_mode: str
    command: str
    target_host: str | None = None


DEFAULT_PROBES = {
    "relay_echo": ProbeSpec(
        probe_id="relay_echo",
        submit_mode="relay",
        command="python3 -c \"print('availability-relay')\"",
    ),
    "ssh_echo": ProbeSpec(
        probe_id="ssh_echo",
        submit_mode="ssh",
        target_host="H20",
        command="python3 -c \"print('availability-ssh')\"",
    ),
    "ssh_h20_echo": ProbeSpec(
        probe_id="ssh_h20_echo",
        submit_mode="ssh",
        target_host="H20",
        command="python3 -c \"print('availability-ssh')\"",
    ),
    "ssh_h20_hostname": ProbeSpec(
        probe_id="ssh_h20_hostname",
        submit_mode="ssh",
        target_host="H20",
        command="hostname",
    ),
    "ssh_h20_nvidia_smi": ProbeSpec(
        probe_id="ssh_h20_nvidia_smi",
        submit_mode="ssh",
        target_host="H20",
        command="nvidia-smi",
    ),
}
