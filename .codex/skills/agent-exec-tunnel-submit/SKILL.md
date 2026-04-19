---
name: agent-exec-tunnel-submit
description: Submit one non-streaming command or upload one shared file set through the AgentExecTunnel dual-repo protocol. Use when Codex should publish a relay command, an ssh-wrapped command, or shared files into the new forward/backward architecture and wait for the final result.
---

# AgentExecTunnel Submit

Use this skill from the repository root at `/workspace/AgentExecTunnel`.

This skill targets the new dual-repo protocol:

- `agent_forward` stores tasks and shared uploaded files
- `agent_backward` stores ACKs and final results

Protocol roles:

- submitter writes forward, reads backward
- executor reads forward, writes backward

Authoritative completion state is always in `agent_backward`.

## Primary Command Interfaces

Relay-host direct command:

```bash
python3 submitter/submit_gitbash.py '<relay_command>'
python3 submitter/submit_powershell.py '<relay_command>'
```

Relay-host ssh-wrapped command:

```bash
python3 submitter/submit_gitbash_ssh.py TARGET_HOST '<target_command>'
python3 submitter/submit_powershell_ssh.py TARGET_HOST '<target_command>'
```

Shared file upload:

```bash
python3 submitter/submit_files.py --name <user_name> --src <local_file_or_dir>
```

Git Bash is the preferred command path unless PowerShell behavior is specifically required.

## Workflow

1. Choose exactly one submit command.
2. Keep the command payload as one whole outer shell string.
3. Expect preview output first.
4. The submitter then waits for a final result in `agent_backward`.
5. For file upload, no task is created; files are copied into `agent_forward/files/<user_name>/...` and pushed.

## Claim Rules

ACK is retained in the protocol, but it is not the caller-facing main output.

Executor-side meaning:

- no result + no ack: claimable task
- ack only: already taken, do not re-run
- result present: terminal

There is no `stale` result in this architecture.

An ack-only task remains suspended until a repair action is applied.

## Preview Output

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

## Shared Files

`submit_files.py` is independent from task submit.

It does this:

1. takes a local file or directory
2. copies it into `agent_forward/files/<user_name>/...`
3. commits and pushes forward

It does not create a task and does not write backward.

Task JSONs do not reference uploaded files as protocol objects. They are a shared material channel that commands may use by convention.

## Examples

```bash
# Relay host direct command through Git Bash
python3 submitter/submit_gitbash.py 'ls /c/Users/'

# Relay host ssh-wrapped command through Git Bash
python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'

# Relay host direct command through PowerShell-compatible path
python3 submitter/submit_powershell.py 'echo hello'

# Relay host ssh-wrapped command through PowerShell-compatible path
python3 submitter/submit_powershell_ssh.py H20 'python3 -c "print(\"ABC\")"'

# Shared file upload
python3 submitter/submit_files.py --name demo --src ./local_dir
```

## Limits

- Task submit and file submit are independent.
- The caller waits only for final result, not for a separate ACK display phase.
- If a task has ACK but no result, it is not automatically reclaimed.
- Recovery for ack-only tasks is manual via repair tooling.
