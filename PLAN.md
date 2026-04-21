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
- [x] 首个 commit：`chore: switch PROGRESS.md to PLAN.md for ntfy-transport`

### 1. 新增 ntfy 传输模块
- [x] `agent_exec_tunnel/ntfy_transport.py`：`publish` / `poll_since` / `poll_loop` / `wait_for` / `seed_seen_ids`（stdlib only，urllib）
- [x] 单元烟雾：直连 ntfy.sh 发一条 + 取一条，回显匹配

### 2. Submitter 侧改造
- [x] `agent_exec_tunnel/submitter.py::publish_task`：去 git，改走 `ntfy_transport.publish(forward_topic, envelope)`；`timeout_seconds` 由调用方必填
- [x] `agent_exec_tunnel/submitter.py::wait_for_result`：改走 `ntfy_transport.wait_for(backward_topic, task_id, cap=timeout/2)`
- [x] `submitter/_submit_common.py::_poll_for_result`：瘦身为 `wait_for` 薄封装；删 `CALLER_POLL_*`、`_read_result_payload`
- [x] `submitter/_submit_common.py::timeout_exit` 文案：ntfy_unreachable→"ntfy 不可达" / healthy→"executor 静默"

### 3. Executor 侧改造
- [x] `agent_exec_tunnel/executor.py::run_loop`：换成 `ntfy_transport.poll_loop(forward_topic, ...)`；启动时一次性 `seed_seen_ids(backward_topic)` 预填
- [x] 新增 `_handle_task_envelope`：内存去重 + 必填 timeout 校验 + 必填 command 校验 + 交给 `_start_worker`
- [x] `_ack_and_start_worker` → `_start_worker`：去掉 ACK 那一半
- [x] `_finalize_result`：改走 `ntfy_transport.publish(backward_topic, envelope)`
- [x] 字段合并：`orphan_ack_at` / `claiming_tasks` / `finished_tasks` → `seen_ids`

### 4. 协议 / 配置清理
- [x] `agent_exec_tunnel/protocol.py`：删 `AckRecord`、`ack_path` / `result_path` / `task_path`、`hour_bucket_parts` / `iter_hour_buckets`；`TaskRecord`/`ResultRecord` 去 `forward_task_path`；`to_envelope()` 加 `kind`
- [x] `agent_exec_tunnel/config.py`：`default_timeout_seconds` 512→300；加 `ntfy_*` 字段；删 `executor_poll_*` / `submit_poll_interval_seconds` / `steady_scan_hours` / `startup_scan_hours` / `executor_backward_write_root` / `backward_root`

### 5. 死代码清理（DoD：rg 为空）
- [x] 删 `GitWriter` 整个类（executor.py）
- [x] 删 `_reconcile_orphan_stale` / `_recover_from_backward` / `startup_scan*` / `scan_recent*` / `_git_sync_once` / `_bucket_glob`
- [x] 删整个 `agent_exec_tunnel/repair.py` + `tools/repair_task.py`（ACK 持久化消失后无意义）
- [x] 删 `tools/bootstrap_repos.py` 里 backward 仓库的 bootstrap 分支；`remotes.py` 去 `backward_url` / `ENV_BACKWARD_REMOTE` / `DEFAULT_BACKWARD_REMOTE`；`.gitignore` 去 `agent_backward/`
- [x] 删基于 git 的老测试：`test_burst_local.py` / `test_fake_relay_roundtrip.py` / `test_fresh_clone.py` / `test_integration_repos.py` / `test_submitter_flow.py` / `runtime_helpers.py`
- [x] 删 `tools/run_burst_live.py` / `tools/run_burst_local_relay.py`（依赖已删除的 helpers）
- [x] 重写 `tests/test_protocol.py` / `tests/test_executor_flow.py` / `tests/test_cli_entrypoints.py` 对新的 ntfy 路径
- [x] `tests/availability/report.py` + `test_availability.py` 去 `ack_latency_s` / `result_latency_s`
- [x] 提交器 CLI 默认 timeout 512→300
- [x] 验收：`rg -n "AckRecord|GitWriter|ack_path|orphan_ack|_reconcile_orphan|startup_scan|scan_recent|executor_backward_write_root|backward_root|backward_url|DEFAULT_BACKWARD|ENV_BACKWARD|iter_hour_buckets|hour_bucket_parts|forward_task_path" --type py` 只剩两条**负向断言**（`settings 不应有 backward_root`、`envelope 不应有 forward_task_path`）

### 6. 验证
- [x] ntfy 单元烟雾通过（独立 topic `aet-smoke-*` 发一条 + 取一条匹配）
- [x] 端到端 relay：`AET_GIT_BASH_EXECUTABLE=/bin/bash python3 submitter/submit_gitbash.py "echo hello-ntfy"` → 7.5s 内返回 stdout 正确，**全程零 git push**
- [x] Timeout 正确性：`--timeout-seconds 5 "sleep 20"` → executor 日志在 5s 后 `finalize status=stale exit=-1`；submitter 端 exit=124 带 "ntfy reachable; executor may be down" 文案
- [x] 崩溃重启幂等：重新构造 `Executor()` 后调 `seed_seen_ids(backward_topic)` 回显 2 个已完成 task_id，证明重启不重跑
- [x] `python3 -m unittest discover tests`：31/31 全绿
- [x] 空闲 10 分钟日志：抖动缓慢上漂、提交新任务立刻回弹 
- [ ] 文件传输回归：`submit_files.py` 正常 push `origin/main` *(未执行 — 需要 forward 仓库 bootstrap 和有效远端；留给运行时验证)*

### 7. 引号地狱 
- [x] submit_gitbash_ssh.py 改为使用 stdin 保护引号
- [x] （替代方案采用 base64 wrap；stdin 路径废弃）
- [ ] submit_gitbash.py 保护引号 *(unchanged — 现状的单引号外裹已够用；复杂 payload 走 submit_gitbash_ssh.py)*

### 8. v0.3：base64 wrap + 统一传输
- [x] `_submit_common.render_gitbash_ssh_command` 改为 base64+$()+bash -c 包装
- [x] preview 函数保持人可读形态（`ssh HOST '<payload>'`），**不**展示 base64
- [x] envelope 去掉 `submit_mode` / `target_host`；metadata 里可选保留 `ssh_host`
- [x] `executor.py::_execution_command` 删除；直接 `task["command"]`
- [x] `submitter.publish_task` / `submit_task` / `_submit_common.submit_and_wait` 去 `submit_mode` / `target_host` 参数
- [x] `MODE_RELAY` / `MODE_SSH` 常量删除
- [x] `command_digest(command, submit_mode, target_host)` → `command_digest(command)`
- [x] availability probe 小改适配（用 ProbeSpec.submit_mode 做 CLI 选择，不进 envelope）
- [x] SKILL.md 所有例子审查；加一句 "preview 行只供阅读，真实发送命令可能经过编码"
- [x] DESIGN.md 末尾：替换 Quoting 章节为 Transport Flow
- [x] README.md 更新 envelope 字段
- [x] 全部测试通过
- [x] VERSION + PACKAGE_VERSION → v0.3；reviews/v0.3.md + evaluations/v0.3.md；tag v0.3

### 8.1 v0.3 post-tag review
- [x] subagent 审查（代码 + docs 对齐）
- [x] bash codex 审查（命令行工具/外部视角）
- [x] 反馈整合 → 确认走 v0.3.1

### 8.2 v0.3.1：preview≠wire 修复 + CLI 拆 Windows/Linux
**触发**：user 报告 `submit_gitbash.py 'ls'` 在 Windows executor 失败（`cmd.exe /c ls` 不认 `ls`）。根因：v0.3 的 `submit_gitbash.py` 仍然提交 raw payload 而非 git-bash wrapped 的 Windows cmdline，preview 与实际 wire 脱节。

- [x] `submit_gitbash.py` 提交 `render_gitbash_relay_command(payload)` 的第一个返回值（`"C:\...bash.exe" -c <payload>` Windows cmdline） — **Windows executor 专用**
- [x] `submit_gitbash_ssh.py` 同步改为提交 `command`（`list2cmdline([git_bash, "-c", relay_script])`） — **Windows executor 专用**
- [x] `submit_powershell.py` 提交 `render_relay_command(payload)` 的第一个返回值（`powershell.exe -EncodedCommand <b64-utf16>`） — **Windows executor 专用**
- [x] `submit_powershell_ssh.py` 保持现状（已经在 v0.3 改为提交 `powershell_cmd`） — **Windows executor 专用**
- [x] 新增 `submitter/submit_bash.py`：单纯提交 raw payload，**Linux executor 专用**（`/bin/sh -c <payload>`）
- [x] `test_cli_entrypoints.py` 更新 submit_gitbash + submit_gitbash_ssh 的断言；补充 submit_bash.py 测试；增加 submit_powershell 的 CLI 测试
- [x] SKILL.md：说明 Windows vs Linux executor 的 CLI 选择；删掉 powershell 路径提及的冗余（如有）
- [x] README / DESIGN：更新 CLI 列表与执行端平台说明
- [x] VERSION / PACKAGE_VERSION → v0.3.1；`reviews/v0.3.1.md` + `evaluations/v0.3.1.md`；tag v0.3.1（3cf3969）

### 9. 已知问题（留给 v0.3+ ）
- [ ] **`submit_files.py` 同步问题**：多 submitter 并发 push 到同一个 forward 仓库 main 分支时存在 git rebase 竞争；当前**暂不可用**于并发场景。单 submitter 场景正常。根因不在 ntfy 转轨，是上古 git 文件平面的老问题。后续考虑：改为 object-store（S3/R2），或每文件一个独立分支/tag。
- [ ] submit_gitbash.py 外层不做 base64 wrap 的权衡回顾（是否也该 base64 化以彻底免引号） — 现在的妥协是：用户单引号外裹 + executor 单层 sh 解析，**足以覆盖大部分场景**。下个周期评估需求。

### 10. v0.4 候选：executor 改 shell=False，直接走首选 shell
**动机**：Python `subprocess.Popen(s, shell=True)` 在 Linux 硬编码走 `/bin/sh -c`，在 Windows 硬编码走 `cmd.exe /c`，多一层永远绕不开。v0.3.1 的 `submit_gitbash.py` 因此必须在 submitter 端先渲染 `"...bash.exe" -c <payload>` 再交给 cmd.exe。

**方案**：executor 加 `Settings.executor_shell`（Linux 默认 `/bin/bash`，Windows 覆盖为 `C:\...\bash.exe`），执行改成 `subprocess.Popen([executor_shell, "-c", task["command"]], shell=False)`。

**收益**：
- envelope.command 回归 raw payload，不再有 CLI 层面的 Windows/Linux 区分
- CLI 数量从 5 个（`submit_{gitbash,gitbash_ssh,powershell,powershell_ssh,bash}.py`）减到 2 个（`submit.py` + `submit_ssh.py`）
- preview ≡ wire ≡ 用户意图，三者完全对齐
- 引号计数少 1 层（cmd.exe/sh 这层没了）

**代价**：
- 需要再次 migrate CLI；v0.3.1 刚教育用户"按 OS 选"又要改回"按场景选"
- envelope 丢失"该用哪个 shell"的信号——如果一台 executor 要同时跑 bash 和 powershell 任务，办不到（需两个 executor 或加 per-task `shell` hint）
- 无法再为每个 submitter CLI 做本地 render 优化（因为 executor 接管了 shell 选择）

**建议**：和 §8.2 deferred 那堆（base64 缺失伪成功保护、ARG_MAX 预检、PowerShell ssh 也 base64 化、host `-` 注入、doc drift、`AET_SHOW_WIRE=1` 调试开关）作为 v0.4 一次性交付。v0.3.1 先沉淀。

## 备注

- 旧 PROGRESS.md 的内容（v0.0.1 → v0.1.3 的历史已完成项 + notes）已经在 git 历史里，不再复制进此文件。
- ACK / 孤儿对账 / at-most-once 语义在本分支**故意**放弃，作为 MVP 交换项；未来若需要再做，入口在 `ntfy_transport.publish` 前后。
- `agent_forward/` 仍走 GitHub `main` 分支的 fetch→rebase→push；消息搬到 ntfy 后，main 上的并发写入只剩文件上传这一处，冲突面大幅下降。
- 两个 ntfy topic 是世界可读的：`agent-forward-285` / `agent-backward-285`。MVP 假设可信环境，鉴权/加密留给后续版本。
- 端到端观测到的延迟（sandbox → ntfy.sh → sandbox）：~7s。主要由 submitter 的 `_poll_for_result` 第一次轮询间隔 + 抖动 + ntfy 消息传播构成，可接受。实际在受限网络上可能更长。
