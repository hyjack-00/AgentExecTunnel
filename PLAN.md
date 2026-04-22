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

### 8.5 v0.3.4：v0.4 deferred 一次性交付 ✅
**动机**：§9 攒了一批 "不急但该做" 的 hardening：远端 `base64` 缺失的伪成功、host 前缀 `-` 的 ssh option 注入、ARG_MAX 没有 pre-flight 只会等到系统层 E2BIG 才报错、`submit_powershell_ssh.py` 仍在用脆弱的 `--%` stop-parsing、preview 与 wire 的 drift 没有调试开关、`tools/test_remote_relay.py` 的 stripper anchor 不够严格。一次打包做掉。

**完成项**：
- [x] `submitter/_submit_common.py::_validate_host()` — 拒绝空字符串、拒绝前缀 `-`（反 ssh option 注入）、限字符集 `[A-Za-z0-9._@:-]+`。`render_gitbash_ssh_command` / `render_ssh_command` 入口都调用
- [x] `_check_arg_max()` + `_ARG_MAX_LIMIT = 100_000`。所有 4 个 render 函数（relay / gitbash_relay / ssh / gitbash_ssh）进入即检查；超限报错指引用户走 `submit_files.py`
- [x] `_build_remote_trampoline(b64)` 统一蹦床构造：前置 `command -v base64 >/dev/null 2>&1 || { … exit 127; }` 检查；解码后 `[ -n "$_s" ] && exec bash -c "$_s" || { … exit 97; }` 避免空字符串时的伪成功；`exec` 直接 propagate exit code
- [x] `render_ssh_command`（PowerShell ssh）改为 base64 蹦床：PS 单引号包裹（`''` 转义内部 `'`），弃用 `--%` stop-parsing。`wrapped_target` 仅为 preview 保留
- [x] `render_gitbash_ssh_command`（Git-Bash ssh）沿用蹦床，升级到带安全检查的版本
- [x] `AET_SHOW_WIRE=1` 调试开关：所有 4 个 `write_*_preview` 在 env 置位时额外 emit `[wire] <full_command>` 行
- [x] `tools/test_remote_relay.py` 的 stripper 改为 `SUBMITTED command_id=` 行 anchor，不再用前缀匹配（payload 里若出现 `  -> ` 也不会误剥）
- [x] 测试补齐：
  - `_validate_host` 的 accept/reject 案例（leading dash / metachar / user@host / port 语法）
  - `_ARG_MAX_LIMIT` 溢出拒绝（4 个 render 都覆盖）
  - PS ssh 的 base64 trampoline 解码断言
  - gitbash ssh 升级后的安全检查断言（exit 127 / 97 / `command -v base64`）
  - `AET_SHOW_WIRE` 行为断言（env 置位产生 `[wire] `、不置位不产生）
- [x] 全测 73 → 73 passing（+6 from v0.3.3 的 67）。本地用 `tests/availability/ssh` shim 手验 gitbash ssh 渲染的 trampoline 可执行，回显正确
- [x] 文档：README Configuration 加 `AET_SHOW_WIRE`；DESIGN.md Trade-offs 段更新 "v0.3.4 closes …"
- [x] VERSION / PACKAGE_VERSION → v0.3.4；reviews + evaluations；tag v0.3.4

### 8.6 v0.4：submit_files.py 同步传输 + 远端拉取验证 ✅
**动机**：v0.3.x 的 `submit_files.py` 只 push 到 GitHub，不阻塞也不验证执行端是否拉取、是否真的有这个文件。user 明确要求：上传必须阻塞到验证有效；本地重试、远端拉取重试都要 15s×3；失败要明确区分 "push 失败"、"pull 失败"、"pull 成功但文件不在"；namespace 一次性。

**方案**：不改 executor，`submit_files.py` 本地渲染远端 bash 命令，走普通 ntfy 任务路径 publish + wait。执行端仍是 mode-agnostic 的 `bash -c <command>`。

- [x] `submit_files.py` 改写：
  - 本地 `git_sync` best-effort + namespace 唯一性检查（`files/<name>/` 已存在则拒绝）
  - `copy_tree_or_file` 落到 `files/<name>/<src.name>`
  - `git_commit_push` 外套 3×15s 重试（内层 `max_attempts=8`），失败 exit 1，提示重跑
  - `_render_remote_verify_command` 生成单串 bash 命令：`FORWARD_ROOT="${AET_FORWARD_ROOT:-agent_forward}"` → `cd` → `git fetch+reset --hard` 三次、每次失败后 `sleep 15` → `[ -e files/<name>/<filename> ]` → stdout `VERIFY_OK …` / stderr `VERIFY_MISSING …`，exit codes 10/11/12 区分 forward_root 缺失 / pull 一直失败 / pull 成功但文件不在
  - `publish_task(timeout_seconds=120)` + `wait_for_result` 阻塞，解析结果：exit 0→`VERIFIED`、exit 12/stderr `VERIFY_MISSING`→exit 3 提示重试、其他→exit 2 打印手动运行的 bash 命令
- [x] 新增 4 个 cli 测试：happy path / 重用 namespace 被拒 / remote pull 失败 with manual hint / local push 失败 with re-run hint
- [x] 端到端手工验证（fake origin git repo）：VERIFY_OK / VERIFY_MISSING / forward_root 缺失三路全部得到正确 exit code
- [x] 73 → 77 passing
- [x] 文档：DESIGN.md 「File plane」章节重写；README "File plane (GitHub + ntfy verification)" + CLI 块同步；PLAN §8.6；reviews + evaluations
- [x] VERSION → v0.4、PACKAGE_VERSION → v0.4、tag v0.4

### 8.7 v0.4.1：executor 侧 wire 预算 + 有界重试 ✅

**触发**：用户澄清 relay host 的 VPN 监管对 HTTP/S 包尺寸有 80–100KB 的硬上限，超了静默丢包。executor → ntfy.sh 的 publish 会不可见地失败；而 `publish_forever` 又是真的 "forever"，worker 线程卡死。

- [x] `Settings.ntfy_result_wire_budget_bytes: int = 60_000`（env：`AET_NTFY_RESULT_WIRE_BUDGET_BYTES`）
- [x] `executor._truncate_result_envelope(envelope, budget)`：tail 超预算时裁剪到"尾巴的尾巴"并前置 `[truncated by executor: original NB, envelope wire budget NB]` 说明
- [x] `ntfy_transport.publish_forever(..., deadline_monotonic=None)`：新增 deadline 参数；过期前/过期后 sleep 之前双重检查，返回 False
- [x] `executor._publish_ack` / `_publish_result` 都把 `task.timeout_seconds` 全额作为重试预算（不做 `timeout - command_elapsed` 减法）
- [x] 11 个新增测试（4 个 deadline + 5 个 truncate + 1 个 integration + 1 个 drive-by fix 陈旧 topic 断言），77 → 88 passing
- [x] DESIGN.md + README.md 更新；reviews/v0.4.1.md + evaluations/v0.4.1.md
- [x] VERSION → v0.4.1；PACKAGE_VERSION → v0.4.1；tag v0.4.1

### 9. 已知问题 & 已解决项备忘

**未解决（留给未来）**：

- [ ] **跨 executor shell 语法挂钩**：一台 executor 只能跑一种 shell（v0.3.2 起 `executor_shell` 配置化、默认 `/bin/bash`）。要同时跑 bash 与 powershell 任务需要两个 executor（不同 ntfy topic），或在 envelope 加 per-task `shell` hint（会打破 v0.3 的 unified transport）。目前无压力解决。
- [ ] **入向 GET 聚合响应可能超过 VPN 审计上限**：executor 的 `poll_since` 响应在 `poll_since="10m"` 窗口内高频任务场景下可累积到接近 audit cap。目前靠窗口缩短缓解，未做 `since=<message-id>` 增量拉取。v0.4.2 候选。
- [ ] **`TailBuffer.limit=4000` 是字符不是字节**：v0.4.1 的 wire budget 在出口兜底覆盖了这个坑，但根本上应把 TailBuffer 改为按字节限。v0.4.2 候选（伴随上面那条一起做）。
- [ ] **企业代理"新域自动隔离 + TTL 自动恢复"下的 ntfy 生存性（观察中）**：华为内网 + netentsec 网关对持续轮询的新域自动隔离 ~几小时、到期自动放行。v0.4.2 调宽 poll 区间到 `[base, 300]s` 观察是否降频率足以绕开触发。若仍被隔离，按优先级上：IT 报备 `ntfy.sh` 白名单 → 切 Gitee 做消息面备份 → Cloudflare Workers + KV 自建 pub/sub → 自建 ntfy 挂自有域。SSE 长连接反而会加速触发，不做。

**已解决（此前记在 §9 但其实已做完或已在 v0.3.4 关闭）**：

- [x] ~~"submit_gitbash.py 外层要不要 base64"~~ — v0.3.2 之后无意义：submit_gitbash.py 提交 raw payload，executor `bash -c <payload>` 只做 1 层 shell 解析，用户单引号外裹就够；再加 base64 会和 submit_gitbash_ssh.py 重复。**无需动作**。
- [x] ~~"executor 也要识别附件化的大附件消息，读取逻辑需在 exe & sub 之间通用化"~~ — 早已集中在 `agent_exec_tunnel/ntfy_transport.py::_record_to_envelope` + `_attachment_maybe_json` + `_load_json_url`。`poll_since()` 是 submitter（`wait_for`）和 executor（`poll_loop` / `seed_seen_ids`）**共用**的入口，attachment 自动解析。**已通用化**。
- [x] ~~"远端 `base64` 缺失的 silent success"~~ — v0.3.4 在蹦床前置 `command -v base64` 检查，缺失时 exit 127 并打印 stderr。
- [x] ~~"`$(…)` 吃尾换行"~~ — 对 shell 命令 payload 无感知（尾 newline 不改变 `bash -c` 语义）；v0.3.4 的 `[ -n "$_s" ]` 检查额外防护了空 decode 的伪成功。
- [x] ~~"ARG_MAX pre-flight"~~ — v0.3.4 统一入口处 100 KB 上限检查，超限给出明确指引走 `submit_files.py`。
- [x] ~~"`submit_powershell_ssh.py` 也 base64 化"~~ — v0.3.4 `render_ssh_command` 用 PS 单引号包裹 base64 蹦床，弃用 `--%`。
- [x] ~~"host 前缀 `-` 的 ssh option 注入"~~ — v0.3.4 `_validate_host` 拒绝。
- [x] ~~"`AET_SHOW_WIRE=1` 调试开关"~~ — v0.3.4 所有 preview writer 支持。
- [x] ~~"`tools/test_remote_relay.py` 的 preview-stripper anchor 不严"~~ — v0.3.4 改为 `SUBMITTED command_id=` 行 anchor。
- [x] ~~"`submit_files.py` 并发 push 同步问题"~~ — submitter 侧有限次 rebase 重试、允许失败，用户明确表示可接受；不是阻塞项。

## 备注

- 旧 PROGRESS.md 的内容（v0.0.1 → v0.1.3 的历史已完成项 + notes）已经在 git 历史里，不再复制进此文件。
- ACK / 孤儿对账 / at-most-once 语义在本分支**故意**放弃，作为 MVP 交换项；未来若需要再做，入口在 `ntfy_transport.publish` 前后。
- `agent_forward/` 仍走 GitHub `main` 分支的 fetch→rebase→push；消息搬到 ntfy 后，main 上的并发写入只剩文件上传这一处，冲突面大幅下降。
- 两个 ntfy topic 是世界可读的：`agent-forward-285` / `agent-backward-285`。MVP 假设可信环境，鉴权/加密留给后续版本。
- 端到端观测到的延迟（sandbox → ntfy.sh → sandbox）：~7s。主要由 submitter 的 `_poll_for_result` 第一次轮询间隔 + 抖动 + ntfy 消息传播构成，可接受。实际在受限网络上可能更长。
