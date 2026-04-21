#!/usr/bin/env python3
"""End-to-end test script for the ssh/relay path.

Exercises a wide range of tricky payloads through `submit_gitbash_ssh.py`
(the base64-trampoline ssh path) and checks the command round-trips
intact. The test cases focus on quoting: single/double/backtick
nesting, `$VAR`, pipes, redirects, heredocs, subshells, multiline,
UTF-8.

Modes
-----

  local (default)
      Uses the fake `ssh` shim at tests/availability/ssh. The shim
      just runs the "remote command" through bash locally, so it
      exercises the entire base64 trampoline round-trip without
      needing a real SSH target. Good for CI / sandbox.

  remote --host HOST
      Exercises the same cases against a real SSH target. Assumes
      `ssh HOST` works (keys set up, host reachable).

Prereq: an executor must already be running (the script does NOT
start one). Typically:

    cd /workspace/AgentExecTunnel.ntfy
    PATH="$(pwd)/tests/availability:$PATH" \\
      python3 -c "from agent_exec_tunnel.executor import Executor; Executor().run_loop()"

    # in another terminal:
    python3 tools/test_remote_relay.py                 # local (shim)
    python3 tools/test_remote_relay.py --host H20      # real
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SUBMITTER = ROOT / "submitter" / "submit_gitbash_ssh.py"
SHIM_DIR = ROOT / "tests" / "availability"


@dataclass(frozen=True)
class Case:
    name: str
    payload: str
    # Either exact stdout match, or a predicate over (stdout, stderr).
    expected_stdout: str | None = None
    check: Callable[[str, str], bool] | None = None
    note: str = ""


def _build_cases() -> list[Case]:
    cases: list[Case] = []

    # --- 基础 ---------------------------------------------------------
    cases.append(Case("basic_echo", "echo hello-ntfy", "hello-ntfy"))
    cases.append(Case("uname", "uname -s", note="just want non-empty",
                      check=lambda o, e: bool(o.strip())))
    cases.append(Case("exit_nonzero", "sh -c 'exit 3'; echo done", "done",
                      note="command chain after non-zero"))

    # --- 单双反引号嵌套 -----------------------------------------------
    cases.append(Case(
        "dquote_inside",
        'echo "a \\"b\\" c"',
        'a "b" c',
    ))
    cases.append(Case(
        "squote_in_dquote",
        'echo "it\'s working"',
        "it's working",
    ))
    cases.append(Case(
        "python_print_escape",
        'python3 -c "print(\\"hello\\nworld\\")"',
        "hello\nworld",
        note="Python source has \\n → real newline",
    ))
    cases.append(Case(
        "python_tuple_dict",
        'python3 -c "print({\\"a\\":1,\\"b\\":[2,3]})"',
        "{'a': 1, 'b': [2, 3]}",
    ))
    cases.append(Case(
        "backtick_date_substitution",
        'echo "the date is $(date +%Y)"',
        note="just want the current year somewhere",
        check=lambda o, e: str(time.gmtime().tm_year) in o or "date" in o,
    ))
    cases.append(Case(
        "backtick_literal",
        "echo 'backtick: `not evaluated`'",
        "backtick: `not evaluated`",
    ))
    cases.append(Case(
        "mixed_quotes_nested",
        """python3 -c "print('it\\\'s \\"complex\\"')" """.strip(),
        note="mixed quotes — any Python-valid interpretation OK",
        check=lambda o, e: "complex" in o and "it" in o,
    ))

    # --- 变量展开 ------------------------------------------------------
    cases.append(Case(
        "var_expansion",
        'echo "$HOME"',
        note="expands on remote",
        check=lambda o, e: bool(o.strip()) and "/" in o,
    ))
    cases.append(Case(
        "var_literal_squote",
        "echo '$HOME'",
        "$HOME",
    ))
    cases.append(Case(
        "var_mix",
        'A=42; echo "A=$A"',
        "A=42",
    ))

    # --- 管道 / 重定向 / subshell -------------------------------------
    cases.append(Case(
        "pipe",
        "echo hello | tr a-z A-Z",
        "HELLO",
    ))
    cases.append(Case(
        "pipe_chain",
        "printf 'a\\nb\\nc\\n' | sort -r | head -n 2",
        "c\nb",
    ))
    cases.append(Case(
        "redirect_and_read",
        "tmp=$(mktemp); echo written > \"$tmp\"; cat \"$tmp\"; rm -f \"$tmp\"",
        "written",
    ))
    cases.append(Case(
        "subshell_cd",
        '(cd /tmp && pwd) | head -n1',
        "/tmp",
    ))
    cases.append(Case(
        "command_substitution",
        'x=$(echo inner); echo "outer[$x]"',
        "outer[inner]",
    ))

    # --- 多行 & heredoc ------------------------------------------------
    cases.append(Case(
        "multiline_with_real_lf",
        "echo line1\necho line2",
        "line1\nline2",
        note="real newlines in payload",
    ))
    cases.append(Case(
        "heredoc",
        "cat <<'EOF'\nfirst\nsecond\nEOF",
        "first\nsecond",
    ))
    cases.append(Case(
        "heredoc_with_vars",
        'n=3; cat <<EOF\ncount=$n\nEOF',
        "count=3",
    ))

    # --- 条件 & 控制流 -------------------------------------------------
    cases.append(Case(
        "and_chain_success",
        "true && echo ok",
        "ok",
    ))
    cases.append(Case(
        "or_chain",
        "false || echo fallback",
        "fallback",
    ))
    cases.append(Case(
        "if_then_fi",
        'if [ 1 -eq 1 ]; then echo yes; else echo no; fi',
        "yes",
    ))
    cases.append(Case(
        "for_loop",
        'for i in 1 2 3; do echo "n=$i"; done',
        "n=1\nn=2\nn=3",
    ))

    # --- Unicode / binary-ish -----------------------------------------
    cases.append(Case(
        "utf8_chinese",
        'echo "中文测试"',
        "中文测试",
    ))
    cases.append(Case(
        "utf8_emoji_and_mix",
        'echo "🚀 a/b テスト"',
        "🚀 a/b テスト",
    ))

    # --- 边界 ---------------------------------------------------------
    cases.append(Case(
        "literal_backslashes",
        'echo "a\\\\b\\\\c"',
        r"a\b\c",
        note="double-backslash in dquote = one literal backslash per pair",
    ))
    cases.append(Case(
        "glob_expansion",
        "cd /tmp && ls *.nonexistent_glob_xyz 2>/dev/null; echo done",
        "done",
    ))
    cases.append(Case(
        "long_payload",
        "printf '%s\\n' " + " ".join(f"word{i}" for i in range(200)) + " | wc -l",
        "200",
        note="~1.5 KB payload",
    ))
    cases.append(Case(
        "nested_python_json",
        """python3 -c 'import json; print(json.dumps({"a":1,"b":[2,3],"s":"x\\"y"}))'""",
        '{"a": 1, "b": [2, 3], "s": "x\\"y"}',
    ))

    return cases


def _normalize_out(s: str) -> str:
    """Strip trailing whitespace per line + final newline for robust compare."""
    return "\n".join(line.rstrip() for line in s.rstrip("\n").split("\n"))


def _run_case(case: Case, host: str, env: dict, timeout: float) -> tuple[bool, str]:
    argv = [
        sys.executable,
        str(SUBMITTER),
        "--timeout-seconds", "60",
        host,
        case.payload,
    ]
    try:
        proc = subprocess.run(
            argv, env=env, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "submitter subprocess timed out"

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    # The submitter prints preview lines and a `SUBMITTED command_id=…` line
    # before the remote stdout. Anchor the split on the `SUBMITTED ` line so
    # a payload whose own output happens to begin with `-> ` or `  -> ` is
    # not misclassified as preview. Drop the `[wire] ` line if AET_SHOW_WIRE
    # was set.
    remote_out_lines: list[str] = []
    seen_submitted = False
    for line in stdout.splitlines(True):
        stripped = line.rstrip("\r\n")
        if not seen_submitted:
            if stripped.startswith("SUBMITTED command_id="):
                seen_submitted = True
            continue
        remote_out_lines.append(line)
    remote_out = "".join(remote_out_lines)

    if case.check is not None:
        ok = case.check(remote_out, stderr)
        if ok:
            return True, ""
        return False, (
            f"check() returned False\n"
            f"  stdout:\n{remote_out!r}\n"
            f"  stderr:\n{stderr!r}"
        )

    assert case.expected_stdout is not None
    got = _normalize_out(remote_out)
    want = _normalize_out(case.expected_stdout)
    if got == want:
        return True, ""
    return False, (
        f"stdout mismatch\n"
        f"  want: {want!r}\n"
        f"  got : {got!r}\n"
        f"  stderr: {stderr.strip()[:400]!r}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host", default="LOCAL",
        help="ssh target host. Default 'LOCAL' uses tests/availability/ssh shim.",
    )
    parser.add_argument(
        "--only", default=None,
        help="run only the case with this name (substring match)",
    )
    parser.add_argument(
        "--subprocess-timeout", type=float, default=120.0,
        help="per-case wall-clock timeout for the submitter (default 120s)",
    )
    parser.add_argument(
        "--stop-on-fail", action="store_true",
        help="stop at first failure",
    )
    args = parser.parse_args()

    env = os.environ.copy()
    if args.host == "LOCAL":
        # Put the ssh shim on PATH so the submitter's ssh resolves to
        # our local stub, AND the executor inherits this too if it's
        # started from this shell (document that separately).
        env["PATH"] = f"{SHIM_DIR}{os.pathsep}{env.get('PATH', '')}"
        env.setdefault("AET_GIT_BASH_EXECUTABLE", "/bin/bash")

    all_cases = _build_cases()
    if args.only:
        cases = [c for c in all_cases if args.only in c.name]
        if not cases:
            print(f"no cases match {args.only!r}", file=sys.stderr)
            return 2
    else:
        cases = all_cases

    print(f"# test_remote_relay: host={args.host} cases={len(cases)}")
    print("#" + "─" * 78)

    started = time.monotonic()
    passed = 0
    failed: list[tuple[Case, str]] = []
    for i, case in enumerate(cases, 1):
        t0 = time.monotonic()
        ok, detail = _run_case(case, args.host, env, args.subprocess_timeout)
        dt = time.monotonic() - t0
        status = "PASS" if ok else "FAIL"
        note = f"  ({case.note})" if case.note else ""
        print(f"[{i:2}/{len(cases)}] {status}  {case.name:<32} {dt:6.2f}s{note}")
        if ok:
            passed += 1
        else:
            failed.append((case, detail))
            for line in detail.splitlines():
                print(f"        {line}")
            if args.stop_on_fail:
                break

    total_dt = time.monotonic() - started
    print("#" + "─" * 78)
    print(f"# passed={passed}/{len(cases)} failed={len(failed)} wall={total_dt:.1f}s")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
