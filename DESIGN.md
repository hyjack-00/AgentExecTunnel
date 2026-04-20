# Design

## Summary

`AgentExecTunnel` is the public control repository for a dual-data-repo execution tunnel:

- `agent_forward`
- `agent_backward`

Repository roles:

- submitter writes `agent_forward`, reads `agent_backward`
- executor reads `agent_forward`, writes `agent_backward`

This architecture does **not** assume a single submitter. Multiple submitters may concurrently publish tasks into forward.

The authoritative state of a task is always in `agent_backward`.

Protocol rules:

- if `result` exists in backward, the task is terminal
- if no `result` exists but `ack` exists in backward, the task has already been taken and must not be reclaimed
- only when neither `result` nor `ack` exists is the task claimable
- `stale` is also a terminal result and means the protocol deadline expired after ACK

Task files are bucketed by hour so executor scan cost stays bounded.

## Public Interfaces

Task submit:

```bash
python3 submitter/submit_powershell.py '<relay_command>'
python3 submitter/submit_powershell_ssh.py TARGET_HOST '<target_command>'
python3 submitter/submit_gitbash.py '<relay_command>'
python3 submitter/submit_gitbash_ssh.py TARGET_HOST '<target_command>'
```

Shared file submit:

```bash
python3 submitter/submit_files.py --name <user_name> --src <local_file_or_dir>
```

Runtime meaning:

- relay submit and ssh submit are different wrapper interfaces at submit time
- once a task document exists in forward, executor treats both as the same class of work: one command to execute
- the only runtime difference is whether executor wraps the command in one relay-side `ssh TARGET_HOST ...`

Executor:

```bash
python3 executor/run_executor.py
python3 executor/run_executor.py --once
```

Repair:

```bash
python3 tools/repair_task.py --task-id ... --clear-ack
python3 tools/repair_task.py --task-id ... --write-failed
```

## Repository Layout

Forward:

- `tasks/YYYY/MM/DD/HH/<task_id>.json`
- `files/<user_name>/...`

Backward:

- `acks/YYYY/MM/DD/HH/<task_id>.json`
- `results/YYYY/MM/DD/HH/<task_id>.json`

`files/<user_name>/...` is a shared material channel. It is independent from task protocol objects.

## Why Synchronization Still Matters

Even though `agent_forward` and `agent_backward` are now single-purpose data repositories, synchronization still matters for correctness.

Reasons:

1. submitter must not decide whether a task was completed based on local memory or a stale local clone
2. executor must not decide whether a task is claimable based on a stale local clone
3. backward is the authority for terminal history, but in the single-executor model steady-state dispatch does not need repeated backward sync
4. forward is the source of new tasks, so executor must sync forward before each scan pass
5. with multiple submitters, forward publication must tolerate concurrent git pushes and converge by retry/rebase

In other words:

- truth is not "what this process remembers"
- truth is not "what the local working tree happened to contain before fetch"
- truth is "what the current synced forward/backward repos say"

## Sequence: Repository Synchronization And Visibility

```mermaid
sequenceDiagram
    autonumber
    participant S1 as Submitter A
    participant S2 as Submitter B
    participant F as agent_forward
    participant B as agent_backward
    participant E as Executor

    Note over S1,E: forward and backward are separate repos with separate working clones

    par multiple submitters may publish
        S1->>F: sync forward
        S1->>B: sync backward
        S1->>F: write task json
        S1->>F: commit + push
    and
        S2->>F: sync forward
        S2->>B: sync backward
        S2->>F: write task json
        S2->>F: commit + push
    end

    loop caller waits for final result
        S1->>B: sync backward
        B-->>S1: no result yet / final result
    end

    E->>F: sync forward
    E->>B: sync backward
    E->>F: read recent task buckets
    E->>B: check result / ack for each task

    alt result exists
        E-->>E: skip task
    else ack exists without result
        E-->>E: skip task, task already taken
    else neither result nor ack exists
        E->>B: write ack
        E->>B: commit + push
        E->>E: execute task
        E->>B: write final result
        E->>B: commit + push
    end
```

Key visibility rules:

- submitter never mutates backward
- executor never mutates forward
- executor must re-check backward before claiming a task
- caller completion is determined only by backward result visibility
- concurrent submitters are allowed, but each submitter must re-sync/rebase if its forward push races with another submitter

## Sequence: Executor Scan And State Update Model

```mermaid
sequenceDiagram
    autonumber
    participant E as Executor
    participant F as agent_forward
    participant B as agent_backward
    participant P as Child Process / One Remote Command

    E->>F: sync forward
    E->>B: sync backward
    E->>F: scan recent hour buckets

    loop for each task in window
        E->>B: check result(task_id)
        alt result exists
            E-->>E: terminal, do nothing
        else no result
            E->>B: check ack(task_id)
            alt ack exists
                E-->>E: already taken, do not reclaim
            else no ack
                E->>B: write ack(task_id)
                E->>B: commit + push
                E->>P: run task
                P-->>E: exit code + output
                E->>B: write result(task_id)
                E->>B: commit + push
            end
        end
    end
```

State model:

- `no result + no ack` => claimable
- `ack only` => taken / running / suspended
- `result present` => terminal
- `stale result present` => terminal, but the local child process may still continue detached

## Windows And Scan Scope

Steady-state scan window:

- recent `6h`

Startup catch-up window:

- recent `72h`

This means:

- routine scan cost stays bounded
- executor restart can still rediscover recent unfinished work
- old history outside the catch-up horizon is not scanned on every pass

## ACK Semantics

ACK is important for executor correctness, not just caller display.

Why:

- if executor only checked for final result, a task that was already taken by another executor but crashed before writing result would look "new"
- that would allow duplicate execution after restart

With ACK retained:

- `ack` marks that some executor path has already claimed the task
- a restarted executor must not treat that task as fresh
- duplicate execution is avoided by protocol rule, not by local memory

This architecture intentionally prefers:

- no duplicate execution

over:

- automatic takeover of ack-only tasks

Recovery of ack-only tasks is manual through repair tooling.

## Relay vs SSH Runtime Semantics

Relay and ssh are not two fundamentally different runtime task categories.

They differ at submit time:

- relay submit publishes the command as-is
- ssh submit publishes metadata that tells executor to add one relay-side ssh wrapper

But once executor has claimed the task, the runtime model is the same:

- take one task
- build one executable command string
- run it
- write one final result

So the core executor state model should be reasoned about as "one claimed command", not as two disjoint task systems.

## Task Lifecycle State Model

The task lifecycle that matters most operationally is:

1. submitter publishes one task document into `agent_forward/tasks/...`
2. executor scans forward and checks backward
3. executor writes one ACK into `agent_backward/acks/...`
4. only after ACK becomes durable should executor treat the task as claimed
5. executor runs the task
6. executor writes one final result into `agent_backward/results/...`
7. submitter returns only after it sees the final result, or after its own local wait times out

Important consequence:

- the submitter does **not** wait for ACK visibility as a user-facing milestone
- the submitter waits only for the final result
- ACK is an executor-side claim marker, not a caller-visible completion signal

## Sequence: Current Task Lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant S as Submitter
    participant F as agent_forward
    participant B as agent_backward
    participant E as Executor
    participant P as Child process

    S->>F: publish task json
    S-->>S: print SUBMITTED

    loop caller waits for final result only
        S->>B: sync backward
        alt result exists
            B-->>S: final result
            S-->>S: return stdout_tail / stderr_tail / exit_code
        else no result yet
            B-->>S: keep waiting or local timeout
        end
    end

    E->>F: scan task buckets
    E->>B: check result / ack
    alt no result and no ack
        E->>B: write ACK
        E->>B: push ACK until durable
        E->>P: start task only after ACK push succeeds
        P-->>E: exit / timeout
        E->>B: write final result
        E->>B: push final result until durable
    else ack exists or result exists
        E-->>E: do not claim
    end
```

State interpretation:

- `forward task exists, no ack, no result` => published but not yet claimed
- `ack exists, no result` => claimed / in progress / suspended after executor-side failure
- `result exists` => terminal

## Sequence: Current Executor Dispatcher / Worker / Writer Model

This section is specifically about what the current dispatcher-only executor does.

```mermaid
sequenceDiagram
    autonumber
    participant Loop as Executor main loop
    participant F as forward clone
    participant W as single git writer
    participant BW as backward-write clone
    participant P as One child process

    Loop->>F: sync + scan
    Loop->>W: enqueue ACK request
    W->>BW: write ACK + commit + push
    W-->>Loop: ACK durable
    Loop->>P: start async worker
    Loop-->>Loop: continue scanning forward only
    P-->>W: final/stale payload
    W->>BW: write result + commit + push
    W-->>P: result durable
```

Current behavior:

- submitter and executor are intentionally asymmetric
- submitter still syncs backward before publish/result trust
- executor does one startup backward recovery sync, then steady-state only scans forward
- ACK is still synchronous from the main loop's perspective
- child execution and finalize are asynchronous worker-owned steps
- all backward writes are serialized through one git writer and one backward-write clone

So the current model is:

- durable ACK first
- then start one async worker
- worker owns `execute -> finalize`
- main loop only dispatches new tasks from forward
- final result or stale result is pushed durably by the single writer

## Sequence: Current Output Visibility Model

```mermaid
sequenceDiagram
    autonumber
    participant P as Child process
    participant E as Executor
    participant B as agent_backward
    participant S as Submitter

    P->>E: stdout / stderr during local execution
    Note over E: current implementation captures output locally only
    Note over B: no incremental output records are published
    E->>B: write one final result with stdout_tail / stderr_tail

    loop submitter poll
        S->>B: poll results/**/*.json
        alt final result visible
            B-->>S: final stdout_tail / stderr_tail
        else not visible yet
            B-->>S: keep waiting
        end
    end
```

Current output semantics:

- there is no protocol-level streaming output
- there is no protocol-level incremental progress event
- executor captures local child output and publishes only one final tail snapshot
- submitter learns output by polling backward for the final result file

## Weak-Network / Intermittent Disconnect Model

The system is designed for networks that may repeatedly disconnect or have long delays.

### Submitter under weak network

Submitter path:

1. sync forward
2. sync backward
3. write and push task into forward
4. poll backward for final result

Behavior under disconnect:

- if submitter cannot sync or push forward, the task is not published
- if a forward push races another submitter push, submitter must fetch/rebase/retry until it either publishes or gives up
- if submitter publishes the task but later cannot read backward, the caller may time out locally
- a caller timeout does not prove the task never ran
- backward remains the source of truth for eventual completion

### Executor under weak network

Executor path:

1. startup sync forward/backward and recover prior ack/result state
2. steady-state sync forward
3. push ack through single writer
4. run task in worker
5. push result through single writer

Behavior under disconnect:

- if executor cannot sync forward, that scan pass cannot make a trustworthy claim decision
- if executor cannot push ACK, it must not proceed as if the task was durably claimed
- if executor already wrote ACK but cannot later push result, the task remains `ack only`
- because `ack only` is not auto-reclaimed, the task is suspended until manual repair
- executor is expected to keep retrying sync / push forever with backoff rather than exiting on transient network loss
- git reconnect is therefore part of the steady-state operating model, not an exceptional shutdown path
- task subprocess timeout must be converted into one durable `stale` result, not an executor crash

This is intentional:

- weak network may delay progress
- weak network must not silently cause duplicate execution

### Practical consequence

Under very poor connectivity, the most likely visible symptoms are:

- delayed task pickup
- delayed final result visibility
- caller-side timeout even though the remote side may have progressed
- accumulation of ack-only tasks if executors can claim but cannot durably write final results
- accumulation of `stale` results for long-running tasks that outlive their deadline
- temporary forward publish contention between multiple submitters

The repair tools exist specifically for this regime.

## Continuously-Running Executor Model

The intended executor behavior is:

- keep running forever
- keep retrying git connectivity forever
- re-establish connections when the network comes back
- never stop the long-running loop just because one sync, push, or task timeout failed

This means the acceptable externally visible failures are:

- submitter-side local timeout
- submitter-side publish failure before a task becomes durable

And the unacceptable internal failures are:

- executor process exit because of transient fetch/push failure
- executor process exit because a task subprocess timed out

## Working Clone Separation

Use separate working clones for:

- submitter
- executor

This is not just a recommendation. It is the supported operating model, including when both roles run on one machine.

Reason:

- if submitter and executor share one working clone, their git operations can interfere with each other
- separate working clones against the same remotes are the expected operating model

Conclusion:

- same remotes: supported
- same working clone: not supported

The local bare-remote integration test in this repository uses this exact separation.

## Sequence: Actual Git Operations On Repositories

This sequence reflects the current implementation more literally than the earlier protocol diagrams.

```mermaid
sequenceDiagram
    autonumber
    participant S as Submitter Process
    participant SF as Submitter forward clone
    participant SB as Submitter backward clone
    participant FO as forward remote
    participant BO as backward remote
    participant E as Executor Process
    participant EF as Executor forward clone
    participant EB as Executor backward clone

    Note over S,E: Supported model = separate working clones, same remotes

    S->>SF: fetch origin/main
    S->>SF: checkout -B main origin/main
    S->>SF: reset --hard origin/main
    S->>SB: fetch origin/main
    S->>SB: checkout -B main origin/main
    S->>SB: reset --hard origin/main
    S->>SF: write task file
    S->>SF: git add + commit
    S->>FO: git push

    loop wait_for_result()
        S->>SB: fetch origin/main
        S->>SB: checkout -B main origin/main
        S->>SB: reset --hard origin/main
        S->>SB: read results/**/*.json
    end

    E->>EF: fetch origin/main
    E->>EF: checkout -B main origin/main
    E->>EF: reset --hard origin/main
    E->>EB: fetch origin/main
    E->>EB: checkout -B main origin/main
    E->>EB: reset --hard origin/main
    E->>EF: read tasks/**/*.json
    E->>EB: check ack/result visibility
    E->>EB: write ack file
    E->>EB: git add + commit
    E->>BO: git push
    E->>E: run command
    E->>EB: write result file
    E->>EB: git add + commit
    E->>BO: git push
    Note over E: on transient git failure, retry forever with backoff
    Note over E: on task timeout, write one stale result and continue loop
```

Key point:

- submitter reads backward by first refreshing its local backward clone
- executor reads forward/backward the same way
- the safe concurrency boundary is the remote repository, not a shared local working tree

## Sequence: Why One Shared Submodule Worktree Can Race

```mermaid
sequenceDiagram
    autonumber
    participant S as Submitter Process
    participant F as shared agent_forward worktree
    participant B as shared agent_backward worktree
    participant E as Executor Process

    Note over S,E: Unsupported model = both processes use the same checked-out submodule worktrees

    par submit path
        S->>F: git_sync(forward)
        S->>B: git_sync(backward)
        S->>F: write task + add + commit + push
    and executor scan
        E->>F: git_sync(forward)
        E->>B: git_sync(backward)
        E->>B: write ack/result + add + commit + push
    end

    Note over F,B: Race is on one repo's HEAD / index / lockfiles / worktree state
    Note over S,E: File-level non-overlap does not remove git-level interference
```

## Fresh-Machine Startup Model

For a new machine, especially a new Windows executor host, the expected path is:

1. clone `AgentExecTunnel`
2. initialize the checked-out submodules:
   - `agent_forward/`
   - `agent_backward/`
3. run:
   - `python3 tools/bootstrap_repos.py`
4. start executor:
   - `python3 executor/run_executor.py`

Bootstrap is expected to verify:

- tunnel repo exists
- repository-local forward/backward submodule working trees exist
- submodules point at reachable origins
- local file-based submodule origins can be repaired into repository-local bare remotes when needed

Fresh-machine readiness therefore depends on:

- correct submodule checkout layout
- submodule reachability
- a Python + git environment that can run the CLI scripts

## Current Test Backing

This repository currently has:

- protocol unit tests
- submit interface compatibility tests
- availability storage/report tests
- local integration tests using real bare git remotes
- a fresh-clone startup smoke path under test development
- fake-relay multi-roundtrip test path under test development
- a 30-second local burst stress path under test development
