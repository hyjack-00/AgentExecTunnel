---
name: agent-exec-tunnel-submit
description: Submit one non-streaming command or upload one shared file set through the AgentExecTunnel dual-repo protocol. Use when Codex should publish a relay command, an ssh-wrapped command, or shared files into the current forward/backward architecture and wait for the final result.
---

# AgentExecTunnel Submit

This skill is for **executing commands through the tunnel** or **uploading shared files**. Use this skill from the repository root at `/workspace/AgentExecTunnel`. 

## Tunnel

- **Local** is the machine where the submitter runs and where the caller waits for results, often using UNIX OS.
  - Submitter run on this machine
- **Relay host** is the machine where the executor actually starts the submitted command, often using Windows OS.
  - Executor run on this machine and will run submitted commands here
  - SSH commands start from the relay host to the target host.
- **Target host** is the remote host alias passed to the ssh-wrapped submit scripts. Examples used in this repository include `H20` and `950`.

## Workflow

1. Run exactly one of the following forms from the repo root. Keep the payload as one whole outer shell string. Do not let bash split the command body into multiple argv pieces.
```bash
# Relay-host direct command:
python3 submitter/submit_gitbash.py '<relay_command>'
python3 submitter/submit_powershell.py '<relay_command>'

# Relay-host ssh-wrapped command:
python3 submitter/submit_gitbash_ssh.py TARGET_HOST '<target_command>'
python3 submitter/submit_powershell_ssh.py TARGET_HOST '<target_command>'

# Shared file upload:
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

Git bash for relay-host command:

```bash
python3 submitter/submit_gitbash.py 'ls /c/Users/'
```

Git bash ssh for remote-host command:

```bash
python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'
```

PowerShell relay with nested quoting:

```bash
python3 submitter/submit_powershell.py '$items = @("A","B"); $items | ForEach-Object { Write-Output ("item=""{0}""" -f $_) }'
```

Git Bash ssh with shell quoting, pipe, semicolon, and embedded Python code:

```bash
python3 submitter/submit_gitbash_ssh.py H20 'printf "%s\n" "$HOME"; ls / | head -5; echo done'
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
python3 submitter/submit_files.py --name local_dir_demo --src ./local_dir
python3 submitter/submit_gitbash.py 'scp -r ./agent_forward/files/local_dir_demo H20:/tmp/remote_dir_demo'
python3 submitter/submit_gitbash_ssh.py H20 'ls /tmp/remote_dir_demo'
```

## What This Skill Does

- `agent_forward` stores submitted tasks and shared uploaded files
- `agent_backward` stores final results
- Submitter writes `agent_forward` and reads `agent_backward`
- Executor reads `agent_forward` and writes `agent_backward`
- Submitter publish commands with limited retries and may failed due to local timeout. 
- Executor publish results with best effort, but results may never arrive to submitter due to local or remote timeout.
- Executor never hard-kill actively tracked tasks, however, a timeout command will not have its result published back.