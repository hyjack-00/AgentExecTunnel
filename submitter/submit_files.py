#!/usr/bin/env python3
"""Synchronous file upload with remote verification.

Flow: local stage → GitHub push (bounded retry) → ntfy-dispatched remote
verify task (pull + file existence check, bounded retry) → report.

Exit codes:
  0 — local upload + remote pull + file check all OK
  1 — local upload to GitHub failed after retries; user should re-run
  2 — local upload OK, remote pull failed or task did not return in time;
      we print the exact bash command the operator can run on the executor
      host to finish the verification manually
  3 — local upload OK, remote pull OK, but the file is absent from the
      pulled tree for an unknown reason; user should retry the upload
"""
from __future__ import annotations

import argparse
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_exec_tunnel.config import default_settings
from agent_exec_tunnel.ntfy_transport import NtfyPublishError
from agent_exec_tunnel.storage import copy_tree_or_file, git_commit_push, git_sync
from agent_exec_tunnel.submitter import publish_task, wait_for_result

UPLOAD_RETRY_ATTEMPTS = 3
UPLOAD_RETRY_DELAY_SECONDS = 15.0
REMOTE_VERIFY_TIMEOUT_SECONDS = 120

EXIT_LOCAL_UPLOAD_FAILED = 1
EXIT_REMOTE_PULL_FAILED = 2
EXIT_FILE_MISSING = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stage a local file or directory into agent_forward/files/<name>/..., "
            "push to GitHub, and wait for the remote executor to pull + verify. "
            "Namespaces are one-shot: a given --name may only be used once."
        ),
        epilog="Example:\n  python3 submitter/submit_files.py --name demo --src ./local_dir",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", required=True, help="one-shot namespace; must not already exist under files/")
    parser.add_argument("--src", required=True, help="local file or directory to upload")
    return parser.parse_args()


def _render_remote_verify_command(namespace: str, filename: str) -> str:
    """Return the bash command the executor runs to pull + verify the upload.

    The command:
    - resolves the executor's agent_forward root from $AET_FORWARD_ROOT (with
      a sensible default of `agent_forward` relative to the executor's cwd)
    - retries `git fetch + reset --hard origin/main` up to 3 times with 15 s
      between attempts
    - prints `VERIFY_OK <namespace>` on stdout when the pulled tree contains
      the expected path, or `VERIFY_MISSING <namespace>` on stderr otherwise
    - uses distinct exit codes so the submitter can disambiguate
      "pull never worked" from "pull worked but file is missing"
    """
    qns = shlex.quote(namespace)
    qfile = shlex.quote(filename)
    # Pre-quote the full relative path once; the remote bash sees it as a
    # single shell-quoted string, no further escaping needed.
    qrelpath = shlex.quote(f"files/{namespace}/{filename}")
    return (
        f'FORWARD_ROOT="${{AET_FORWARD_ROOT:-agent_forward}}" && '
        f'cd "$FORWARD_ROOT" || {{ echo "forward_root not found: $FORWARD_ROOT" >&2; exit 10; }} && '
        f'('
        f'  git fetch --quiet origin main && git reset --quiet --hard origin/main'
        f' || ( sleep 15 && git fetch --quiet origin main && git reset --quiet --hard origin/main )'
        f' || ( sleep 15 && git fetch --quiet origin main && git reset --quiet --hard origin/main )'
        f' || {{ echo "git pull failed after 3 attempts" >&2; exit 11; }}'
        f') && '
        f'( [ -e {qrelpath} ] '
        f'  && echo "VERIFY_OK namespace={namespace} path={qrelpath}" '
        f'  || {{ echo "VERIFY_MISSING namespace={namespace} path={qrelpath}" >&2; exit 12; }} '
        f')'
    )


def _manual_retry_hint(verify_cmd: str) -> str:
    return (
        "to finish verification manually, run this on the executor host:\n"
        f"  bash -c {shlex.quote(verify_cmd)}"
    )


def _sync_forward_root(forward_root: Path) -> None:
    """Best-effort `git fetch + reset --hard origin/main` with one quick retry.

    Failure here is not fatal — we still try to copy and push; push with
    rebase loop absorbs the conflict. But a successful sync makes the
    subsequent namespace-uniqueness check reflect the true remote state.
    """
    try:
        git_sync(forward_root)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(
            f"warning: pre-upload git_sync failed (continuing): {exc.stderr.strip() if exc.stderr else exc}\n"
        )


def _push_with_retry(forward_root: Path, message: str) -> bool:
    """Run `git_commit_push` up to UPLOAD_RETRY_ATTEMPTS times, waiting
    UPLOAD_RETRY_DELAY_SECONDS between attempts. Returns True on success."""
    for attempt in range(1, UPLOAD_RETRY_ATTEMPTS + 1):
        try:
            # Inner rebase retry is capped small (8) so each outer attempt is
            # responsive; the outer 15 s sleep absorbs transient networks.
            git_commit_push(forward_root, message, max_attempts=8)
            return True
        except subprocess.CalledProcessError as exc:
            err = exc.stderr.strip() if exc.stderr else repr(exc)
            sys.stderr.write(
                f"git push attempt {attempt}/{UPLOAD_RETRY_ATTEMPTS} failed: {err}\n"
            )
            if attempt < UPLOAD_RETRY_ATTEMPTS:
                time.sleep(random.uniform(UPLOAD_RETRY_DELAY_SECONDS * 0.8, UPLOAD_RETRY_DELAY_SECONDS * 1.2))
    return False


def _run_remote_verify(namespace: str, verify_cmd: str) -> tuple[int | None, str, str]:
    """Publish the verify task over ntfy and block for the result.

    Returns (exit_code, stdout_tail, stderr_tail). `exit_code is None`
    signals that the task never produced a final envelope within budget
    (ntfy outage, executor down, or pull took longer than the task
    timeout). All three caller-visible failure modes are derivable from
    the return shape without inspecting the envelope `status` field.
    """
    try:
        task_id = publish_task(
            command=verify_cmd,
            timeout_seconds=REMOTE_VERIFY_TIMEOUT_SECONDS,
            metadata={"kind": "submit_files_verify", "namespace": namespace},
            emit_submitted=False,
        )
    except NtfyPublishError as exc:
        sys.stderr.write(f"ntfy publish of verify task failed: {exc}\n")
        return None, "", ""

    sys.stdout.write(f"SUBMITTED verify_task_id={task_id}\n")
    sys.stdout.flush()

    try:
        result = wait_for_result(
            task_id=task_id,
            result_timeout_seconds=REMOTE_VERIFY_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        sys.stderr.write(f"{exc}\n")
        return None, "", ""

    payload = result.payload
    exit_code = payload.get("exit_code")
    status = payload.get("status")
    if status == "stale":
        # Executor saw the task but wall-clock timeout fired before the
        # subprocess finished; treat as "pull failed / never completed".
        return None, payload.get("stdout_tail") or "", payload.get("stderr_tail") or ""
    return exit_code, payload.get("stdout_tail") or "", payload.get("stderr_tail") or ""


def main() -> None:
    args = parse_args()
    settings = default_settings()
    src = Path(args.src).resolve()
    if not src.exists():
        raise SystemExit(f"source path does not exist: {src}")

    # Namespace uniqueness is enforced locally only. Sync first so a
    # namespace someone else pushed is visible here.
    _sync_forward_root(settings.forward_root)

    namespace_dir = settings.forward_root / "files" / args.name
    if namespace_dir.exists():
        raise SystemExit(
            f"namespace {args.name!r} is already in use under {namespace_dir}; "
            f"namespaces are one-shot — pick a different --name"
        )

    # Stage locally.
    dst = namespace_dir / src.name
    copy_tree_or_file(src, dst)

    # Push with bounded retry.
    if not _push_with_retry(settings.forward_root, f"upload files for {args.name}"):
        sys.stderr.write(
            f"local upload to GitHub failed after {UPLOAD_RETRY_ATTEMPTS} attempts "
            f"({int(UPLOAD_RETRY_DELAY_SECONDS)}s between retries); "
            f"please re-run `python3 submitter/submit_files.py --name {args.name} --src {args.src}`\n"
        )
        raise SystemExit(EXIT_LOCAL_UPLOAD_FAILED)

    relpath = dst.relative_to(settings.forward_root).as_posix()
    print(f"UPLOADED src={src} dst={relpath}")

    # Render the remote verify command and dispatch.
    verify_cmd = _render_remote_verify_command(args.name, src.name)
    exit_code, _stdout, stderr_tail = _run_remote_verify(args.name, verify_cmd)

    if exit_code == 0:
        print(f"VERIFIED namespace={args.name} — local upload + remote pull + file check all OK")
        return

    # Distinguish "pull OK, file missing" from "pull never worked". The
    # remote script emits "VERIFY_MISSING" on the former and exits 12.
    pull_ok_file_missing = exit_code == 12 or "VERIFY_MISSING" in stderr_tail

    if pull_ok_file_missing:
        sys.stderr.write(
            f"local upload to GitHub succeeded; remote pull succeeded; "
            f"but files/{args.name}/{src.name} is absent from the pulled tree. "
            f"unknown cause — please retry the upload with a new --name.\n"
        )
        raise SystemExit(EXIT_FILE_MISSING)

    # Anything else = pull never worked (or the task never returned).
    sys.stderr.write(
        f"local upload to GitHub succeeded; remote pull did NOT succeed "
        f"(exit_code={exit_code}).\n"
    )
    if stderr_tail.strip():
        sys.stderr.write(f"remote stderr:\n{stderr_tail.rstrip()}\n")
    sys.stderr.write(f"\n{_manual_retry_hint(verify_cmd)}\n")
    raise SystemExit(EXIT_REMOTE_PULL_FAILED)


if __name__ == "__main__":
    main()
