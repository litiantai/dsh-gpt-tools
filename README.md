# DeepSeek ↔ GPT 阻塞式监工

## 启动、状态、停止

在包含两个 Python 文件的目录执行（Python 3、zstd、已登录的 codex CLI）：

```sh
python3 -B dsh_bridge.py start
python3 -B dsh_bridge.py status
python3 -B dsh_bridge.py stop
```

默认监听 `127.0.0.1:13081`，日志与结果保存在脚本上一级的 `work/dsh-bridge/`。不自动开机启动。后台进程只扫描会话目录，扫描不调用模型。保留 `dsh_supervisor.py` 作为日志解析依赖，但不要同时启动旧版单向监工。

## 工作协议

让 DeepSeek 在方案确认前、实现阶段节点、最终验收时分别调用：

```sh
python3 -B /绝对路径/dsh_bridge.py handoff --phase plan --summary-file /绝对路径/plan.md
python3 -B /绝对路径/dsh_bridge.py handoff --phase checkpoint --summary-file /绝对路径/progress.md
python3 -B /绝对路径/dsh_bridge.py handoff --phase acceptance --summary-file /绝对路径/acceptance.md
```

每次作为**唯一的前台 bash 调用**，`timeoutMs=600000`、`run_in_background=false`，工作目录必须是当前会话的项目根目录。Harness 自动提供 `DSH_SESSION_ID`。等待期间不得并行开发、启动子代理或后台任务。若工具被意外提升后台，只能等待其结果，不得继续开发。

响应决策：

- `approve`：方案批准，执行返回的 `instruction`。
- `revise`：按返回指令修改，重新交接。
- `done`：验收通过，结束任务。
- `blocked`：模型故障、超时或额度耗尽，停止工作并报告。

DeepSeek 的 HTTP 请求保持阻塞；Codex 完成后，返回值直接成为 DeepSeek 的工具结果。这是唤醒与恢复的双向通道，无需浏览器轮询或 Harness 认证 Cookie。默认每次审查上限 240 秒、每天最多 12 次；同一 `--request-id UUID` 的重试返回缓存。预算不够时不自动放行。

## 范围与限制

服务发现所有已有和新增会话，但**发现会话不会自动让它遵循协议**。需要给要托管的任务加入以上交接指令。此版本没有修改 Harness 的全局提示词，也没有强制暂停任意不合作的会话。一次处理一个审查；批量并发使用前应扩大客户端等待策略。

当前审查器针对 `packages/dsh-games` 配置：只读实现、可运行测试和构建，默认使用本机 CLI 已实测可用的 gpt-5.5、low 推理等级；可通过启动参数 `--model` 指定其他可用模型。审查器不自动修改游戏；纠偏指令由 DeepSeek 实施。通用项目需要修改审查范围和文件证据采集范围。

`pause_proof` 比较审查前后的游戏文件哈希，并检查 DeepSeek 是否新增 `step/start` 或 `request/header`。这证明本次阻塞期间没有新模型步骤；不会终止交接前已启动的后台进程，任务必须遵循禁止后台工作的协议。文件哈希排除 lib/dist/node_modules 等产物目录，因此审查器运行构建不会被误判为 DeepSeek 继续写源代码。

仅绑定本机，使用本机状态目录里的令牌认证；令牌不写入任务提示词。状态目录权限为 700。审查失败会返回 blocked，不自动恢复开发。
