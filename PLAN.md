# Plan — ntfy-transport

## 上下文

- 目标：把任务下发 + 结果返回从 GitHub 搬到 ntfy（topic: `agent-forward-285` / `agent-backward-285`）；文件传输仍走 GitHub。
- 两端默认 timeout = 300s；executor 空闲轮询抖动上限 = timeout/2 = 150s，~1h 内到达。
- 参考完整设计：`/home/agent-user/.claude/plans/ntfy-github-hidden-taco.md`
- 旧 PROGRESS.md 的历史（v0.0.1 → v0.1.3）已经在 git 历史里（本 commit 的父节点之前），不复制进这里。

## Todo

### 0. 分支与文档骨架
- [x] 建 worktree `../AgentExecTunnel.ntfy`，分支 `ntfy-transport`
- [x] `git mv PROGRESS.md PLAN.md` 并用本骨架覆盖
- [ ] 首个 commit：`chore: switch PROGRESS.md to PLAN.md for ntfy-transport`

### 1. 新增 ntfy 传输模块
- [ ] `agent_exec_tunnel/ntfy_transport.py`：`publish` / `poll_since` / `poll_loop` / `wait_for`（stdlib only，urllib）
- [ ] 单元烟雾：直连 ntfy.sh 发一条 + 取一条，打印回显

### 2. Submitter 侧改造
- [ ] `agent_exec_tunnel/submitter.py::publish_task`：去 git，改走 `ntfy_transport.publish(forward_topic, envelope)`；`timeout_seconds` 由调用方必填
- [ ] `agent_exec_tunnel/submitter.py::wait_for_result`：改走 `ntfy_transport.wait_for(backward_topic, task_id, cap=timeout/2)`
- [ ] `submitter/_submit_common.py::_poll_for_result`：瘦身为 `wait_for` 薄封装；删 `CALLER_POLL_*`、`_read_result_payload`
- [ ] `submitter/_submit_common.py::timeout_exit` 文案：sync→"ntfy 不可达" / healthy→"executor 静默"

### 3. Executor 侧改造
- [ ] `agent_exec_tunnel/executor.py::run_loop`：换成 `ntfy_transport.poll_loop(forward_topic, ...)`；启动时一次性 `poll_since(backward_topic)` seed `seen_ids`
- [ ] 新增 `_handle_task_envelope`：内存去重 + 必填 timeout 校验 + 交给 `_start_worker`
- [ ] `_ack_and_start_worker` → `_start_worker`：去掉 ACK 那一半
- [ ] `_finalize_result`：改走 `ntfy_transport.publish(backward_topic, envelope)`
- [ ] 字段合并：`orphan_ack_at` / `claiming_tasks` / `finished_tasks` → `seen_ids`

### 4. 协议 / 配置清理
- [ ] `agent_exec_tunnel/protocol.py`：删 `AckRecord`、`ack_path`；`TaskRecord`/`ResultRecord` 去 `forward_task_path`；`to_json()` 加 `kind`
- [ ] `agent_exec_tunnel/config.py`：`default_timeout_seconds` 512→300；加 `ntfy_*` 字段；删 `executor_poll_*` / `submit_poll_interval_seconds` / `steady_scan_hours` / `startup_scan_hours` / `executor_backward_write_root`

### 5. 死代码清理（DoD：rg 为空）
- [ ] 删 `GitWriter` 整个类（executor.py，约 150 行）
- [ ] 删 `_reconcile_orphan_stale` / `_recover_from_backward` / `startup_scan*` / `scan_recent*` / `_git_sync_once` / `_bucket_glob`
- [ ] 删 `tools/bootstrap_repos.py` 里 backward 仓库的 bootstrap 分支
- [ ] 删/改 `tests/` 里断言 ACK/结果文件落盘的用例
- [ ] 验收：`rg -n "AckRecord|GitWriter|ack_path|orphan_ack|_reconcile_orphan|startup_scan|executor_backward_write_root" agent_exec_tunnel/ submitter/ tests/` 为空

### 6. 验证
- [ ] bootstrap 本 worktree 的 sibling clones：`python tools/bootstrap_repos.py`（只 forward）
- [ ] ntfy 单元烟雾通过
- [ ] 端到端 relay：`python submitter/submit_relay.py "echo hello-ntfy"` ≲4s 返回，全程无 git push
- [ ] Timeout 正确性：`--timeout 60 "sleep 120"` → 60s 后 `failed/stale`
- [ ] 空闲 10 分钟日志：抖动缓慢上漂、提交新任务立刻回弹
- [ ] 崩溃重启幂等：2h 窗口内已完成任务不重跑
- [ ] 文件传输回归：`submit_files.py` 正常 push `origin/main`
- [ ] `pytest tests/ -x` 全绿

## 备注

- 旧 PROGRESS.md 的内容（v0.0.1 → v0.1.3 的历史已完成项 + notes）已经在 git 历史里，不再复制进此文件。
- ACK / 孤儿对账 / at-most-once 语义在本分支**故意**放弃，作为 MVP 交换项；未来若需要再做，入口在 `ntfy_transport.publish` 前后。
- `agent_forward/` 仍走 GitHub `main` 分支的 fetch→rebase→push；消息搬到 ntfy 后，main 上的并发写入只剩文件上传这一处，冲突面大幅下降。
- 两个 ntfy topic 是世界可读的：`agent-forward-285` / `agent-backward-285`。MVP 假设可信环境，鉴权/加密留给后续版本。
