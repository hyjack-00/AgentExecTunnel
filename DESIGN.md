# Design

## Overview

`AgentExecTunnel` is a dual-repository command tunnel built around two data repos:

- `agent_forward`: submit-side publication channel
- `agent_backward`: executor-side acknowledgement and result channel

The system is designed for weak-network environments where Git connectivity may flap but later recover. The main goal is to keep the executor alive, keep task state durable, and avoid shared-worktree corruption.

## Goals

- Support multiple concurrent submitters publishing tasks into one forward repo
- Keep one long-running executor alive across transient Git and network failures
- Make task claim and task completion visible through durable Git commits
- Avoid running submitter and executor in the same Git worktree
- Keep the protocol simple: publish task, claim with ACK, publish final result

## Non-Goals

- Protocol-level streaming output
- Multi-executor coordination on the same forward/backward remote pair
- Shared-worktree submitter/executor deployment
- Strong exactly-once guarantees across multiple executors

## Architecture

### Repository Roles

- Submitter writes `agent_forward`
- Submitter reads `agent_backward`
- Executor reads `agent_forward`
- Executor writes `agent_backward`

Terminal task truth is always in `agent_backward`.

### Runtime Components

- `submitter/*.py`
  - build relay or SSH command wrapper
  - sync repos before publish
  - publish task
  - poll only for final result
- `executor/run_executor.py`
  - runs the long-lived executor loop
  - scans forward for claimable tasks
- `agent_exec_tunnel/executor.py`
  - dispatcher logic
  - durable ACK path
  - async worker lifecycle
  - single backward writer
- `agent_exec_tunnel/storage.py`
  - Git sync / commit / push primitives
- `tools/run_burst_local_relay.py`
  - two isolated whole-repo local integration pressure test
- `tools/run_burst_live.py`
  - submit-pressure tool against an already-running remote executor

## Repository Layout

### Forward

- `tasks/YYYY/MM/DD/HH/<task_id>.json`
- `files/<user_name>/...`

### Backward

- `acks/YYYY/MM/DD/HH/<task_id>.json`
- `results/YYYY/MM/DD/HH/<task_id>.json`

## Task State Model

The protocol is intentionally small.

- No ACK, no result: task is claimable
- ACK exists, result absent: task has been claimed
- Result exists: task is terminal
- `stale` is terminal and means the protocol deadline expired

The submitter does not wait for ACK. It waits only for the final result record.

## Core Assumptions

- Multiple submitters may race to publish into `agent_forward`
- Exactly one executor is active for one forward/backward remote pair
- Submitter and executor run in separate working clones
- Transient Git failures are expected and must be retried
- Executor liveness is more important than immediate completion

## Submit Path

1. Build the final relay or SSH command string
2. Sync `agent_forward`
3. Sync `agent_backward`
4. Write task JSON into `agent_forward/tasks/...`
5. Commit and push task publication
6. Print `SUBMITTED ...`
7. Poll `agent_backward/results/...` until result or caller timeout

Important behavior:

- Submit publish is bounded-retry
- Submit-side waiting is bounded by caller timeout
- Caller timeout does not prove the task never ran

## Executor Path

1. Startup recovery syncs backward once
2. Executor scans recent forward task buckets
3. For each claimable task, publish durable ACK first
4. Only after durable ACK, start one async worker
5. Worker runs `execute -> finalize`
6. Final result is written durably to backward through the single writer

Important behavior:

- Steady-state dispatch syncs only `agent_forward`
- Backward writes are serialized
- Timeout becomes a durable `stale` result
- The main scan loop does not poll running child state

## Concurrency Model

### Submitter Concurrency

Concurrent submitters are supported at the remote-repo level.

They are **not** supported in one shared submitter worktree, because publication still uses Git operations that mutate one index / HEAD / lock set.

### Executor Concurrency

The runtime model is intentionally single-executor.

Why:

- startup imports backward ACK/result state once
- steady-state duplicate suppression then relies on local in-memory sets
- no distributed lease or compare-and-swap protocol exists across executors

Running multiple executors against the same forward/backward remotes is therefore out of scope.

## Failure Model

### Weak Network

Submitter side:

- publish may fail before a task becomes durable
- result polling may fail even after the task was accepted
- caller may time out while the executor later still finishes the task

Executor side:

- fetch/push failures are retried forever with exponential backoff
- temporary disconnects must not terminate the process
- writer initialization and steady-state writes both retry until recovery

### Timeout

Task timeout is protocol-visible:

- the executor writes one durable `stale` result
- the local child process may still continue detached
- no second final result is later published

### Interrupted Finalize

If the executor is interrupted after ACK is durable but before final result is durable, the visible state may remain `ack only`.

That state is currently not auto-reclaimed.

## Operational Constraints

### Supported

- Separate submitter and executor clones
- Same forward/backward remotes
- Multiple submitters
- Long-running executor under intermittent network failure

### Unsupported

- Shared submitter/executor worktree
- Multiple executors on one remote pair
- Streaming protocol output

## Sequence: Submit / ACK / Finalize

```mermaid
sequenceDiagram
    autonumber
    participant S as Submitter
    participant F as agent_forward
    participant B as agent_backward
    participant E as Executor
    participant P as Child process

    S->>F: sync forward
    S->>B: sync backward
    S->>F: write task + commit + push
    Note over S: print SUBMITTED

    E->>F: sync + scan
    alt task is claimable
        E->>B: write ACK + push
        E->>P: start task
        P-->>E: exit or deadline
        E->>B: write final/stale result + push
    else task already claimed or finished
        Note over E: skip task
    end

    loop until caller timeout or result exists
        S->>B: sync backward
        B-->>S: final result or no result yet
    end
```

## Sequence: Dispatcher / Worker / Writer

```mermaid
sequenceDiagram
    autonumber
    participant L as Executor loop
    participant F as forward clone
    participant W as git writer
    participant BW as backward-write clone
    participant T as task worker

    L->>F: sync + scan
    L->>W: enqueue ACK write
    W->>BW: commit + push ACK
    W-->>L: ACK durable
    L->>T: start async worker
    Note over T: execute command
    T->>W: enqueue final/stale result
    W->>BW: commit + push result
```

## Sequence: Why Shared Worktree Is Unsafe

```mermaid
sequenceDiagram
    autonumber
    participant S as Submitter
    participant F as shared forward worktree
    participant B as shared backward worktree
    participant E as Executor

    par submit path
        S->>F: fetch / checkout / reset
        S->>B: fetch / checkout / reset
        S->>F: add / commit / push
    and executor path
        E->>F: fetch / checkout / reset
        E->>B: fetch / checkout / reset
        E->>B: add / commit / push
    end

    Note over F,B: race is on one worktree, one index, and one set of Git lockfiles
```

## Timing and Retry Policy

- Executor poll backoff: `1s -> 2s -> 4s -> 8s`
- Executor Git command timeout: `10s`
- Submitter publish retry: bounded
- Executor sync/push retry: infinite
- Default task timeout: `512s`

## Validation Focus

The repository currently emphasizes:

- submit interface tests
- executor flow tests
- fresh-clone bootstrap coverage
- dual-checkout integration coverage
- local relay burst pressure runs
- live submit-pressure observation
