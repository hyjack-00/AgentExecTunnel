# Progress

## Active Todo

- [x] Freeze old `agent_forward` monolith on `legacy/agent_forward-v4.5-monolith`
- [x] Rebuild `agent_forward` main as data-only forward repo
- [x] Initialize `agent_backward` main as data-only backward repo
- [x] Create `AgentExecTunnel` root docs for `v0.0.1`
- [x] Add submodules for the data repos
- [x] Implement bootstrap tool
- [x] Implement task submit tool
- [x] Implement file submit tool
- [x] Implement executor scan/ack/result flow
- [x] Implement repair tool
- [x] Add initial unit tests
- [x] Add `reviews/v0.0.1.md`
- [x] Add `evaluations/v0.0.1.md`
- [x] Migrate availability suite into the new repository layout
- [x] Add broader local integration coverage against real local repos/remotes
- [x] Run review/evaluation and release `v0.0.1`
- [x] Restore submit-layer interfaces matching legacy agent_forward entrypoints
- [x] Add submit interface compatibility tests against legacy render/preview shape
- [x] Add repository-local skill for the new AgentExecTunnel submit workflow
- [x] Rewrite `DESIGN.md` to use sequence diagrams instead of the current static flowcharts
- [x] Add explicit repository synchronization / visibility model to `DESIGN.md`
- [x] Explain dual-repo synchronization guarantees in docs even though forward/backward are single-purpose repos
- [x] Add a weak-network / intermittent-disconnect behavior section to `DESIGN.md`
- [x] Document how submitter and executor behave under repeated fetch/push failures
- [x] Clarify in docs that relay and ssh are not two fundamentally different runtime task types; both become one remote command, with the difference mainly in submit-layer wrapping
- [x] Remove the outdated single-submit assumption; define and support multi-submitter publish semantics
- [x] Add forward publish retry/self-heal so multiple submitters can concurrently publish unique task commits
- [x] Document the recommended deployment rule that submitter and executor should use separate working clones
- [x] Add a fresh-machine startup path review for a new Windows executor host
- [x] Ensure a fresh clone on a new machine can bootstrap and start `executor/run_executor.py`
- [x] Add and keep local fake-relay roundtrip tests with multiple back-and-forth task cycles
- [x] Add and keep a 30-second local burst stress path in the new architecture
- [x] Actually run the 30-second local burst stress path and record the result
- [x] Add a real submodule-backed burst runner so pressure runs can leave visible task/ack/result files under `agent_forward/` and `agent_backward/`
- [x] Remove duplicate `tools/submit_files.py`; keep `submitter/submit_files.py` as the single file-upload entrypoint
- [x] Add CLI tests around submitter / repair entrypoints
- [ ] Add configurable SSH probe presets for availability
- [x] Review whether a single shared working clone is acceptable when submitter and executor run on one machine
- [x] Rename submodule working directories to `agent_forward/` and `agent_backward/` and align docs with the sibling repo names
- [x] Switch `.gitmodules` to explicit GitHub HTTPS URLs
- [x] Change default runtime roots to repository-local submodule paths
- [x] Add repo-operation sequence diagrams explaining supported separate clones vs unsupported shared worktrees
- [x] Make bootstrap repair local file-based submodule origins into repo-local bare remotes
- [x] Restore legacy-style infinite retry / backoff for executor git sync and push paths
- [x] Keep executor scan loop alive across transient git/network failures
- [x] Convert executor command timeout into durable failed result instead of process crash
- [x] Document the continuously-running executor model in `DESIGN.md`
- [x] Clarify the actual task lifecycle: durable ACK first, then one blocking task execution, then durable final result
- [x] Clarify that current output visibility is final-result polling, not protocol-level streaming
- [ ] If desired later: reintroduce active-process async monitoring for long tasks instead of the current one-task blocking model


## Notes

- Version line is restarted from `v0.0.1` because this is a new architecture.
- ACK is retained and is the executor-side claim marker.
- Real local integration is currently covered with separate submitter/executor working clones against the same bare remotes.
- The current 30-second local burst diagnostic passed with `30/30` completed tasks using separate submitter clones and a fake relay ssh shim.
- Same remotes are supported; a shared submitter/executor working clone is not the supported deployment model.
- `.gitmodules` now declares explicit HTTPS origins for both data submodules.
- Executor now follows the intended long-running model: transient git/network failures are retried with backoff and task timeout is finalized as one durable failed result.
- Current executor is still single-task blocking after claim; it does not yet maintain an active in-memory async task set.
