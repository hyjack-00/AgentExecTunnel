# Progress

## Active Todo

- [x] Freeze old `agent_forward` monolith on `legacy/agent_forward-v4.5-monolith`
- [x] Rebuild `agent_forward` main as data-only forward repo
- [x] Initialize `agent_backward` main as data-only backward repo
- [x] Create `AgentExecTunnel` root docs for `v0.0.1`
- [x] Add submodules for `forward/` and `backward/`
- [x] Implement bootstrap tool
- [x] Implement task submit tool
- [x] Implement file submit tool
- [x] Implement executor scan/ack/result flow
- [x] Implement repair tool
- [x] Add initial unit tests
- [x] Add `reviews/v0.0.1.md`
- [x] Add `evaluations/v0.0.1.md`
- [ ] Migrate availability suite into the new repository layout
- [ ] Add broader local integration coverage against real local repos/remotes
- [x] Run review/evaluation and release `v0.0.1`

## Notes

- Version line is restarted from `v0.0.1` because this is a new architecture.
- ACK is retained and is the executor-side claim marker.
- `stale` is removed from protocol.
- Availability remains part of the target repository layout, but the first cut only carries the core protocol code and tests.
