---
name: agent-exec-tunnel-submit
description: Submit one non-streaming command or upload one shared file set through the AgentExecTunnel. Use when Codex should publish a relay command, an ssh-wrapped command, or shared files and wait for the final result.
---

# AgentExecTunnel Submit

This skill is for **executing commands through the tunnel** or **uploading shared files**. Use this skill from the repository root at `/workspace/AgentExecTunnel`.

As of v0.2+, task envelopes and result envelopes flow over ntfy.sh (topics `agent-forward-285` / `agent-backward-285`). File uploads still go through the `agent_forward` git repo. Task / result transport does not involve git at all.

Note: the terminal "preview" output below is **for humans**. The actual command on the wire may be encoded/wrapped to protect quoting; the preview shows the semantic intent of the command, not the literal bytes transmitted.

## Tunnel

- **Local** is the machine where the submitter runs and where the caller waits for results, often using UNIX OS.
  - Submitter run on this machine
- **Relay host** is the machine where the executor actually starts the submitted command, often using Windows OS.
  - Executor run on this machine and will run submitted commands here
  - SSH commands start from the relay host to the target host.
- **Target host** is the remote host alias passed to the ssh-wrapped submit scripts. Examples used in this repository include `H20` and `950`.

## Workflow

**All submitter CLIs ship a single command string to the executor.** The executor then runs `<configured_shell> -c <command>` (typically bash / Git Bash). The variants differ only in what shape of string they generate:

| CLI | What it ships | Use when |
|---|---|---|
| `submit.py` | exactly your payload, no wrapping | you want full manual control |
| `submit_bash.py` / `submit_gitbash.py` | your payload, no wrapping | a bash / git-bash command |
| `submit_powershell.py` | `powershell.exe -EncodedCommand <b64>` | a PowerShell command |
| `submit_gitbash_ssh.py` | `ssh HOST "bash -c $(echo '<b64>' | base64 -d)"` | complex quoting through ssh — variants below do NOT chew quotes |
| `submit_powershell_ssh.py` | powershell → ssh wrapper | ssh from a PowerShell-flavored relay |

The `_ssh` variants are **convenience wrappers**. You can always reproduce their effect with `submit.py` plus manual quoting — they just save you from counting shell layers yourself.

1. Run exactly one of the following forms from the repo root. Keep the payload as one whole outer shell string. Do not let bash split the command body into multiple argv pieces.

```bash
# Bottom of stack — raw, no rendering:
python3 submitter/submit.py '<any shell command>'

# Convenience: direct bash/git-bash command:
python3 submitter/submit_bash.py '<relay_command>'
python3 submitter/submit_gitbash.py '<relay_command>'

# Convenience: PowerShell command:
python3 submitter/submit_powershell.py '<ps_command>'

# Convenience: ssh-wrapped with bullet-proof quoting (uses base64):
python3 submitter/submit_gitbash_ssh.py TARGET_HOST '<target_command>'

# Shared file upload (single-submitter scenarios only — see known issues):
python3 submitter/submit_files.py --name <namespace> --src <local_file_or_dir>
```

2. Wait for a SUBMITTED line from CLI output, to confirm durable submit.
3. Wait for the final result from CLI output. Do not expect any protocol-level streaming output. For long running commands, inspect the remote log or output files for progress and intermediate results.
4. Final result may never arrive locally. In that case the CLI exits with a timeout-style error; for side-effecting work, inspect durable remote output or status before retrying.

## Contract

- This is a blocking, non-streaming submission interface. The CLI usually returns the final result, but callers must handle timeout.
- Safety Rules: BE CAREFUL WITH RETRYING.
  - A local timeout does not prove that nothing ran remotely.
  - For side-effecting work, should always verify durable remote evidence before retrying.
  - For long-running work, should inspect durable remote log output or status to verify progress and success.
- Task submit and file submit are independent, but later task can see files uploaded by former file submit (see File Transfers).
- Prefer Git Bash command path over PowerShell.
- Prefer small, explicit commands over complex shell metaprogramming like `eval`, command substitution chains, or self-reparsing shell tricks.
- For long-running work, prefer a new inspecting command that can be polled for progress and final success, rather than relying on a single long-running command with a big timeout. This is because the executor will publish a durable stale result on timeout, which may never arrive to submitter, and in that case submitter should not assume the command did not run at all.


## Output Expectations

Preview output appears first.

Git Bash relay:

```text
-> "C:\Program Files\Git\bin\bash.exe" -c "<relay_command>"
  -> <relay_command>
```

Git Bash ssh:

```text
-> "C:\Program Files\Git\bin\bash.exe" -c "ssh TARGET_HOST '<target_command>'"
  -> ssh TARGET_HOST '<target_command>'
    -> <target_command>
```

PowerShell relay:

```text
-> powershell.exe -EncodedCommand <preview>
  -> <relay_command>
```

PowerShell ssh:

```text
-> powershell.exe -EncodedCommand <preview>
  -> ssh TARGET_HOST --% <wrapped_target_command>
    -> <target_command>
```

After durable submit, the caller sees one `SUBMITTED ...` line and later receives only the final stored stdout/stderr tails plus the final exit status.
There is no protocol-level streaming output in this skill.


## Examples

Linux executor, relay command:

```bash
python3 submitter/submit_bash.py 'ls -la /tmp'
python3 submitter/submit_bash.py 'uname -a && hostname'
```

Windows executor, relay command:

```bash
python3 submitter/submit_gitbash.py 'ls /c/Users/'
```

Git bash ssh for remote-host command:

```bash
python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'
```

Git Bash ssh with shell quoting, pipe, semicolon, and embedded Python code (all quoting is preserved end-to-end):

```bash
python3 submitter/submit_gitbash_ssh.py H20 'printf "%s\n" "$HOME"; ls / | head -5; echo done'
python3 submitter/submit_gitbash_ssh.py H20 'python3 -c "print(\"hello\nworld\")"'
python3 submitter/submit_gitbash_ssh.py H20 'python3 -c '"'"'import json; print(json.dumps({"path":"/tmp/demo","text":"A\"B","items":["x","y"]}))'"'"''
```

Shared file upload:

```bash
python3 submitter/submit_files.py --name local_dir_demo --src ./local_dir
```

## File Transfers

`submit_files.py` is independent from task submit. It does not create a task and does not write backward.

It does this:

1. takes a local file or directory
2. copies it into `agent_forward/files/<namespace>/...`
3. commits and pushes forward

File upload and verification example:

```bash
python3 submitter/submit_files.py --name transfer_demo --src ./skills
python3 submitter/submit_gitbash.py 'scp -r ./agent_forward/files/transfer_demo/skills H20:/tmp/transfer_demo'
python3 submitter/submit_gitbash_ssh.py H20 'ls /tmp/transfer_demo'
```

## What This Skill Does

- Task / result envelopes ride ntfy.sh topics `agent-forward-285` (submit → executor) and `agent-backward-285` (executor → submit). No git on the message path.
- Only `agent_forward` git repo is involved — for file uploads under `agent_forward/files/<namespace>/...`.
- The envelope carries **one plain command string**; every submitter flavor (gitbash / gitbash-ssh / powershell / ...) renders its own wrapping client-side. Executor is mode-agnostic.
- Submitter publish has bounded retry; on final failure you see `publish rejected; command was not published` and exit 1.
- Executor publishes results with infinite retry (blocks the worker thread until ntfy accepts); results rarely disappear unless the executor itself is killed mid-publish.
- Executor never hard-kills actively tracked tasks; a timeout emits a `stale` result and leaves the subprocess detached.