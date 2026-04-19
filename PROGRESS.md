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
- [ ] Add CLI tests around submitter / repair entrypoints
- [ ] Add configurable SSH probe presets for availability
- [ ] Review whether a single shared working clone is acceptable when submitter and executor run on one machine
- [ ] 解释：为什么 submodule 目录仍使用 `forward/` 和 `backward/`，以及是否值得改成 `agent_forward/` / `agent_backward/`

## Notes

- Version line is restarted from `v0.0.1` because this is a new architecture.
- ACK is retained and is the executor-side claim marker.
- `stale` is removed from protocol.
- Availability has been migrated into the new repository layout.
- Real local integration is currently covered with separate submitter/executor working clones against the same bare remotes.
- Submit interfaces are now exposed under `submitter/` with the legacy PowerShell/Git Bash naming pattern.
- The current 30-second local burst diagnostic passed with `30/30` completed tasks using separate submitter clones and a fake relay ssh shim.
