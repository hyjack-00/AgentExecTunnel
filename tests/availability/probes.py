from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    submit_mode: str
    command: str
    target_host: str | None = None
    implies_ok: tuple[str, ...] = ()


DEFAULT_PROBES = {
    "relay_echo": ProbeSpec(
        probe_id="relay_echo",
        submit_mode="relay",
        command="python3 -c \"print('availability-relay')\"",
        implies_ok=("relay",),
    ),
    "ssh_echo": ProbeSpec(
        probe_id="ssh_echo",
        submit_mode="ssh",
        target_host="H20",
        command="python3 -c \"print('availability-ssh')\"",
        implies_ok=("relay", "H20"),
    ),
    "ssh_h20_echo": ProbeSpec(
        probe_id="ssh_h20_echo",
        submit_mode="ssh",
        target_host="H20",
        command="python3 -c \"print('availability-ssh')\"",
        implies_ok=("relay", "H20"),
    ),
    "ssh_h20_hostname": ProbeSpec(
        probe_id="ssh_h20_hostname",
        submit_mode="ssh",
        target_host="H20",
        command="hostname",
        implies_ok=("relay", "H20"),
    ),
    "ssh_h20_nvidia_smi": ProbeSpec(
        probe_id="ssh_h20_nvidia_smi",
        submit_mode="ssh",
        target_host="H20",
        command="nvidia-smi",
        implies_ok=("relay", "H20"),
    ),
    "ssh_950_echo": ProbeSpec(
        probe_id="ssh_950_echo",
        submit_mode="ssh",
        target_host="950",
        command="python3 -c \"print('availability-950')\"",
        implies_ok=("relay", "950"),
    ),
    "ssh_910_echo": ProbeSpec(
        probe_id="ssh_910_echo",
        submit_mode="ssh",
        target_host="910",
        command="python3 -c \"print('availability-910')\"",
        implies_ok=("relay", "910"),
    ),
}


def all_tags() -> list[str]:
    seen: dict[str, None] = {}
    for spec in DEFAULT_PROBES.values():
        for tag in spec.implies_ok:
            seen.setdefault(tag, None)
    return list(seen.keys())
