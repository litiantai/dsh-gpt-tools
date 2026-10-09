# DeepSeek Harness 旧版监工命令行

> 本页描述旧版 `dsh_supervisor.py`，不是当前看板启动的服务。新版插件按方案、异常和验收自动强制监管，见 [README](README.md#插件自动强制监管)。不要与新版共用状态目录并行运行。

监听本机 DeepSeek Harness 的所有现有工作区以及之后新建的会话。程序直接读取 `~/.dsh/sessions` 的持久化事件日志；启动时把已有事件设为基线，不会为旧任务调用 Codex。

## 触发规则

- 每个会话的轮次结束：唤醒 Codex 审核用户要求、最终回答和工作区证据。
- 每 25 个工作步骤：唤醒一次检查进度。
- 同一轮次最近 5 次工具调用中至少 3 次失败：唤醒一次排查。
- 同一会话默认 10 分钟冷却；每天最多 12 次 Codex 审查，避免高频调用。

Codex 只在发现具体问题时尝试通过 computer use 向对应 DeepSeek 会话发送纠偏指令。它不会自动推送代码、部署或执行其他对外高影响操作。审查文本与调用轨迹保存在运行状态目录。

## 使用

本机需要 `python3`、`zstd`、`codex` 命令，以及正在运行的 DeepSeek Harness。以下命令可以在任意目录执行，建议显式指定状态目录：

```bash
python3 /Users/ranrui/Documents/Codex/2026-09-30/new-chat-2/outputs/dsh_supervisor.py start --state-dir /Users/ranrui/Documents/Codex/2026-09-30/new-chat-2/work/dsh-supervisor
python3 /Users/ranrui/Documents/Codex/2026-09-30/new-chat-2/outputs/dsh_supervisor.py status --state-dir /Users/ranrui/Documents/Codex/2026-09-30/new-chat-2/work/dsh-supervisor
python3 /Users/ranrui/Documents/Codex/2026-09-30/new-chat-2/outputs/dsh_supervisor.py stop --state-dir /Users/ranrui/Documents/Codex/2026-09-30/new-chat-2/work/dsh-supervisor
```

可用 `--max-per-day`、`--cooldown-minutes`、`--checkpoint-steps` 调整调用频率。先用 `start --dry-run` 验证事件发现、不实际唤醒 Codex；切换到正式模式前先运行 `stop`。`events.jsonl` 记录触发与跳过原因，`status.json` 记录最新扫描状态，`reviews/` 存放 Codex 审查结果。

该版本监听本机文件，不依赖网页认证。Harness 如果迁移会话存储路径或日志格式，需要相应调整读取器。computer use 是否能在非交互 Codex 执行中操作已登录的 Harness 页面，需在首次实际事件触发时验证；如果不可用，审查结果仍会记录建议的纠偏文字。
