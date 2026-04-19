from __future__ import annotations

from pathlib import Path

from .config import Settings, default_settings
from .protocol import ResultRecord, command_digest, iso_z, utc_now
from .storage import git_commit_push, git_sync, read_json, write_json


def _find_task_paths(settings: Settings, task_id: str) -> tuple[Path | None, Path | None]:
    ack = next(settings.backward_root.glob(f"acks/**/*.json"), None)
    result = next(settings.backward_root.glob(f"results/**/*.json"), None)
    found_ack = None
    found_result = None
    for path in settings.backward_root.glob("acks/**/*.json"):
        if path.name == f"{task_id}.json":
            found_ack = path
            break
    for path in settings.backward_root.glob("results/**/*.json"):
        if path.name == f"{task_id}.json":
            found_result = path
            break
    return found_ack, found_result


def clear_ack(task_id: str, settings: Settings | None = None) -> None:
    cfg = settings or default_settings()
    git_sync(cfg.backward_root)
    ack_path, _ = _find_task_paths(cfg, task_id)
    if ack_path is None:
        raise FileNotFoundError(f"ack not found task_id={task_id}")
    ack_path.unlink()
    git_commit_push(cfg.backward_root, f"clear ack {task_id}")


def write_failed(task_id: str, exit_code: int = 1, stderr_tail: str = "manual repair", settings: Settings | None = None) -> None:
    cfg = settings or default_settings()
    git_sync(cfg.backward_root)
    ack_path, result_path = _find_task_paths(cfg, task_id)
    if result_path is not None:
        raise FileExistsError(f"result already exists task_id={task_id}")
    if ack_path is None:
        raise FileNotFoundError(f"ack not found task_id={task_id}")
    ack = read_json(ack_path)
    now = utc_now()
    result = ResultRecord(
        task_id=task_id,
        forward_task_path=ack["forward_task_path"],
        executor_id="repair",
        status="failed",
        started_at=ack["ack_at"],
        finished_at=iso_z(now),
        exit_code=exit_code,
        stdout_tail="",
        stderr_tail=stderr_tail,
        command_digest=ack["command_digest"],
    )
    result_path = cfg.backward_root / Path("results") / Path(ack["forward_task_path"]).relative_to("tasks")
    write_json(result_path, result.to_json())
    git_commit_push(cfg.backward_root, f"repair failed result {task_id}")
