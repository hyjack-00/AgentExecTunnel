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

### 8.3 v0.3.2：executor 改 shell=False + envelope 保持 string + submit.py ✅

**触发**：user 重新澄清需求：envelope 字符串传输（secondary preference）；relay 要彻底去掉 cmd.exe 这一层；ssh 变体 CLI 仅作便利工具，与手写 `ssh HOST '...'` 用户侧效果等价；EXTRA：submit.py 可通过复杂手写 payload 达成任何变体等效。

**方案**（字符串 envelope + shell=False + 配置 shell，**不是** argv-list）：
- [x] `agent_exec_tunnel/config.py` 新增 `executor_shell: str`（默认 `/bin/bash`）和 `executor_shell_args: list[str]`（默认 `["-c"]`），env 可覆盖 `AET_EXECUTOR_SHELL` / `AET_EXECUTOR_SHELL_ARGS`
- [x] `agent_exec_tunnel/executor.py`：`Popen(task["command"], shell=True)` → `Popen([cfg.executor_shell, *cfg.executor_shell_args, task["command"]], shell=False)`
- [x] `agent_exec_tunnel/protocol.py` 保持 `command: str`（envelope 不动）
- [x] `submitter/submit_gitbash.py` 回滚 v0.3.1 的客户端 wrap：现在只提交 **raw payload**
- [x] `submitter/submit_gitbash_ssh.py` 回滚 v0.3.1：提交 `relay_script`（base64 蹦床保留）而不是 Windows cmdline
- [x] `submitter/submit_powershell.py`、`submit_powershell_ssh.py`：保持 v0.3.1
- [x] `submitter/submit_bash.py`：保持 v0.3.1
- [x] **新增** `submitter/submit.py`：最薄 CLI，envelope 就是用户输入字节
- [x] 测试：回滚 v0.3.1 test_cli_entrypoints.py 的 "wrapped cmdline" 断言为 "raw payload"，新增 submit.py 测试；新增 executor Popen shell=False 的断言
- [x] **新增** `tools/test_remote_relay.py`：31 个复杂 payload 测试脚本（单双反引号嵌套、`$VAR`、pipe、redirect、heredoc、subshell、UTF-8、多行、literal `\`、glob、长 payload、nested Python JSON），走 submit_gitbash_ssh → fake ssh 或真实远端。支持 `--host`、`--only`、`--stop-on-fail`
- [x] 文档：DESIGN.md Transport flow 改为新链路 + 更新 parse-count 表；SKILL.md CLI 表改为"按便利程度选"、submit.py 置顶；README 同步 Configuration 章节
- [x] VERSION / PACKAGE_VERSION → v0.3.2；reviews/v0.3.2.md + evaluations/v0.3.2.md；tag v0.3.2

### 8.4 v0.3.3：ntfy 非匿名访问（hardcoded token）✅
**动机**：公共 ntfy.sh 匿名 tier 有 rate limit（HTTP 429）+ 端口耗尽（Errno 99）。user 要在代码里留一个"自己填 token"的位置，让 submitter + executor 都以带 `Authorization: Bearer <token>` 头访问私有 ntfy 或 ntfy Pro。

- [x] `agent_exec_tunnel/ntfy_transport.py` 顶部加硬编码常量 `NTFY_AUTH_TOKEN = ""`（用户自填）；`AET_NTFY_TOKEN` env 可覆盖
- [x] 每个 HTTP 请求（`_publish_once` / `poll_since` / `_load_json_url`）带 `Authorization: Bearer <token>` 头，当常量为空时不带（维持匿名行为）
- [x] submitter + executor 都经 `ntfy_transport` 一个入口，改一处生效
- [x] 单元测试：空 token 无 Authorization 头；非空 token 有头（5 个 AuthHeaderTests）
- [x] 文档：README Configuration + DESIGN.md "No auth" → "partially closed in v0.3.3"
- [x] VERSION → v0.3.3、reviews + evaluations、tag v0.3.3

### 9. 已知问题 & 已解决项备忘

**未解决（留给 v0.4+）**：

- [ ] **`submit_files.py` 并发 push 同步问题**：多 submitter 并发向同一 `agent_forward` main 分支 push 会撞 rebase/push 循环；单 submitter 场景正常。根因是 git 文件平面的老问题，与 ntfy 转轨无关。后续可选：改 object-store（S3/R2）或每文件一个独立分支/tag。

- [ ] **v0.4 candidates**（打包一次性做）— 远端 `base64` 缺失的 silent success 保护、`$(…)` 吃尾换行的文档 / 替代蹦床、ARG_MAX pre-flight、`submit_powershell_ssh.py` 也 base64 化、host 前缀 `-` 的 ssh option 注入、`AET_SHOW_WIRE=1` 调试开关。`tools/test_remote_relay.py` 的 preview-stripper 用 `SUBMITTED command_id=…` 行作为 anchor（当前用前缀匹配，payload 内容如果撞上 `  -> ` 会误剥）。

**已解决（此前记在 §9 但其实已做完）**：

- [x] ~~"submit_gitbash.py 外层要不要 base64"~~ — v0.3.2 之后无意义：submit_gitbash.py 提交 raw payload，executor `bash -c <payload>` 只做 1 层 shell 解析，用户单引号外裹就够；再加 base64 会和 submit_gitbash_ssh.py 重复。**无需动作**。
- [x] ~~"executor 也要识别附件化的大附件消息，读取逻辑需在 exe & sub 之间通用化"~~ — 早已集中在 `agent_exec_tunnel/ntfy_transport.py::_record_to_envelope` + `_attachment_maybe_json` + `_load_json_url`。`poll_since()` 是 submitter（`wait_for`）和 executor（`poll_loop` / `seed_seen_ids`）**共用**的入口，attachment 自动解析。**已通用化**。

### 10. v0.4 候选：其余 deferred 一次性做完
**动机**：Python `subprocess.Popen(s, shell=True)` 在 Linux 硬编码走 `/bin/sh -c`，在 Windows 硬编码走 `cmd.exe /c`，多一层永远绕不开。v0.3.1 的 `submit_gitbash.py` 因此必须在 submitter 端先渲染 `"...bash.exe" -c <payload>` 再交给 cmd.exe。

**方案**：executor 加 `Settings.executor_shell` 和 `Settings.executor_shell_args`（不只一个字段——bash 用 `["-c"]`，PowerShell 用 `["-NoProfile", "-Command"]`，cmd.exe 用 `["/c"]`），执行改成：
```python
subprocess.Popen(
    [cfg.executor_shell, *cfg.executor_shell_args, task["command"]],
    shell=False,
)
```

**收益**（经 re-audit 后的诚实清单）：
- executor 本地 shell 层数从 2（Python 的 cmd.exe/sh + 目标 bash）降到 1
- envelope.command 回归 raw payload，CLI 可以压到 2 个（`submit.py` + `submit_ssh.py`）
- preview ≡ wire ≡ 用户意图，三者完全对齐
- Windows cmd.exe 的 argv 拼接怪癖（`\"`、CreateProcess-to-argv 规则）整层消失
- 不过 **ssh 路径的总 shell 层数不变**（远端 sshd 的 `$SHELL -c` + 最终 `bash -c decoded` 两层是 ssh 协议决定的、客户端免不掉），**base64 蹦床仍必需**

**代价**：
- 需要再次 migrate CLI；v0.3.1 刚教育用户"按 OS 选"又要改回"按场景选"
- envelope 丢失"该用哪个 shell"的信号：**一台 executor 只能一种 shell**。要同时跑 bash 和 powershell 任务需两个 executor（不同 ntfy topic），或加 per-task `shell` hint（打破 unification）
- 跨 shell 语法 payload 必挂：bash executor 不认 `dir`/`Get-Location`；powershell executor 不认 `ls`。控制权从 "per-task 指定" 换成 "per-executor 配置"——**不是问题消失、是位置转移**
- ARG_MAX 上限不变（argv 总字节数限制一样生效）
- 远端无 `base64` 命令时仍会伪成功，**shell=False 救不了远端**

**建议**：和 §8.2 deferred 那堆（base64 缺失伪成功保护、ARG_MAX 预检、PowerShell ssh 也 base64 化、host `-` 注入、doc drift、`AET_SHOW_WIRE=1` 调试开关）作为 v0.4 一次性交付。v0.3.1 先沉淀。

**v0.3.1 仍然有价值的部分**（shell=False 迁移后留下）：
- submitter 侧的 `render_gitbash_ssh_command` 的 **base64 蹦床**在 ssh 路径仍必需（解 quoting）
- preview vs wire 的清晰分野：shell=False 让两者更对齐，但 base64 蹦床下仍然"preview 是人意图、wire 是 base64"，SKILL.md 的那句 caveat 保留

## 备注

- 旧 PROGRESS.md 的内容（v0.0.1 → v0.1.3 的历史已完成项 + notes）已经在 git 历史里，不再复制进此文件。
- ACK / 孤儿对账 / at-most-once 语义在本分支**故意**放弃，作为 MVP 交换项；未来若需要再做，入口在 `ntfy_transport.publish` 前后。
- `agent_forward/` 仍走 GitHub `main` 分支的 fetch→rebase→push；消息搬到 ntfy 后，main 上的并发写入只剩文件上传这一处，冲突面大幅下降。
- 两个 ntfy topic 是世界可读的：`agent-forward-285` / `agent-backward-285`。MVP 假设可信环境，鉴权/加密留给后续版本。
- 端到端观测到的延迟（sandbox → ntfy.sh → sandbox）：~7s。主要由 submitter 的 `_poll_for_result` 第一次轮询间隔 + 抖动 + ntfy 消息传播构成，可接受。实际在受限网络上可能更长。
