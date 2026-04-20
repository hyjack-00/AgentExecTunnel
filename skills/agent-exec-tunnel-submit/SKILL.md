---
name: agent-exec-tunnel-submit
description: Submit one non-streaming command or upload one shared file set through the AgentExecTunnel dual-repo protocol. Use when Codex should publish a relay command, an ssh-wrapped command, or shared files into the current forward/backward architecture and wait for the final result.
---

# AgentExecTunnel Submit

Use this skill from the repository root at `/workspace/AgentExecTunnel`.

This skill is for **executing one command through the tunnel** or **uploading one shared file set**. It is not a general design document.

## What This Skill Knows

- `agent_forward` stores submitted tasks and shared uploaded files
- `agent_backward` stores final results
- submitter writes `agent_forward` and reads `agent_backward`
- executor reads `agent_forward` and writes `agent_backward`
- the caller waits for the final result only

Do not mention internal protocol stages that are not caller-facing.

## Terms

### Relay Host

The **relay host** is the machine where the executor actually starts the submitted command.

- relay commands run directly on that machine
- ssh commands also start on that machine, then add one `ssh TARGET_HOST ...` hop

### TARGET_HOST

`TARGET_HOST` is the remote host alias passed to the ssh-wrapped submit scripts.

Examples used in this repository include `H20` and `950`.

Treat `TARGET_HOST` as an already-known ssh target name. Do not invent new host names unless the user explicitly gives one.

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

## Safety Rules

Default to conservative behavior.

- Prefer idempotent and side-effect-free commands
- Do not lightly submit commands that create, delete, rewrite, move, or chmod files
- Do not lightly submit commands that start background services, containers, long-lived daemons, or jobs
- Do not lightly submit commands that change git state, branch state, remotes, or working trees
- Do not lightly submit commands that alter system configuration, credentials, permissions, or network settings
- Do not treat shared uploaded files as a safe place for secrets, credentials, or large binary payloads

If the user asks for a side-effecting operation, do not silently “helpfully” broaden it. Keep the command narrow and literal.

If the user request is ambiguous and could rewrite files or mutate state, stop and ask for confirmation instead of guessing.

## Command Construction Rules

1. Choose exactly one submit command.
2. Keep the command payload as one whole outer shell string.
3. Prefer the narrowest command that satisfies the request.
4. Expect preview output first.
5. The submitter then waits for a final result in `agent_backward`.
6. For file upload, no task is created; files are copied into `agent_forward/files/<user_name>/...` and pushed.

Important:

- Do not split the payload across multiple shell arguments.
- Do not wrap an already-complete ssh command inside another ssh command unless the user explicitly wants nested ssh.
- Do not rewrite the user into a different execution mode unless needed.

## Output Expectations

Preview output appears first.

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

After durable submit, the caller sees one `SUBMITTED ...` line and later receives only the final stored stdout/stderr tails plus the final exit status.

There is no protocol-level streaming output in this skill.

## Shared Files

`submit_files.py` is independent from task submit.

It does this:

1. takes a local file or directory
2. copies it into `agent_forward/files/<user_name>/...`
3. commits and pushes forward

It does not create a task and does not write backward.

Task JSONs do not reference uploaded files as protocol objects. They are only a shared material channel by convention.

## Examples

Simple relay command through Git Bash:

```bash
python3 submitter/submit_gitbash.py 'ls /c/Users/'
```

Simple remote command through Git Bash:

```bash
python3 submitter/submit_gitbash_ssh.py H20 'nvidia-smi'
```

PowerShell relay with nested quoting:

```bash
python3 submitter/submit_powershell.py '$items = @("A","B"); $items | ForEach-Object { Write-Output ("item=""{0}""" -f $_) }'
```

PowerShell ssh with nested Python quoting:

```bash
python3 submitter/submit_powershell_ssh.py H20 'python3 -c "import json; print(json.dumps({\"msg\":\"hello\",\"items\":[1,2,3]}))"'
```

Git Bash ssh with shell quoting, pipe, and semicolon:

```bash
python3 submitter/submit_gitbash_ssh.py H20 'printf "%s\n" "$HOME"; ls / | head -5; echo done'
```

Git Bash ssh with embedded Python code and mixed quotes:

```bash
python3 submitter/submit_gitbash_ssh.py H20 'python3 -c '"'"'import json; print(json.dumps({"path":"/tmp/demo","text":"A\"B","items":["x","y"]}))'"'"''
```

Shared file upload:

```bash
python3 submitter/submit_files.py --name demo --src ./local_dir
```

## Quoting Guidance

- Use one outer shell string for the payload
- For Git Bash ssh mode, treat the payload as the remote shell command
- For PowerShell ssh mode, the submitter will generate one relay-side `ssh TARGET_HOST --% ...` wrapper
- Prefer small, explicit commands over shell metaprogramming
- Avoid `eval`, command substitution chains, or self-reparsing shell tricks unless the user explicitly asks for them

When the user provides a Linux-only reference command, adapt it to the submit script shape instead of copying it literally.

## Limits

- Task submit and file submit are independent
- The caller waits only for final result
- File upload is for shared materials, not secrets or bulk transport
- This skill is for one-shot command submission, not streaming sessions
