# 运行与故障处理

## 本机服务

在 skill 资源基目录执行（或使用脚本绝对路径）：

```sh
python3 -B scripts/bridge.py start
python3 -B scripts/bridge.py status
python3 -B scripts/bridge.py stop
```

`start` 可重复执行。skill 在首次使用时启动服务，所以用户通常只需在 DeepSeek 调用 `/dsh-gpt-supervisor` 并描述任务。服务不自动开机启动；重启电脑后再次使用 skill 会重新启动。

所有已有和新增会话都会被轻量发现；只有加载本 skill、执行交接命令的任务会进入暂停和审查流程。skill 是协作协议，不会终止其他会话或此前已启动的后台写入进程。

## 参数

- `--model` / `DSH_SUPERVISOR_MODEL`：审查模型。默认 gpt-5.5 是这台机器上已真实验证可用的 CLI 模型。其他账号应选择其 CLI 可用模型；启动已运行服务后改变模型需先停止、再带参数启动。
- `--codex-bin`：Codex CLI 可执行文件路径，默认从 PATH 查找。
- `--home`：会话日志根目录，默认 `$DSH_HOME` 或 `~/.dsh`。
- `--state-dir` / `DSH_SUPERVISOR_STATE`：令牌、pid、事件与审查证据目录。
- `--port`：默认 13083；改变后 start/status/handoff 均应传相同参数。
- `--review-timeout`：默认 240 秒，最大 450 秒。含排队等待必须小于前台 Bash 的 600 秒期限。
- `--max-per-day`：默认每日 12 次请求。quota.json 保留每日使用次数；不要删除或重置来绕过额度。
- `--scope`：可重复指定项目内相对路径；Git 项目通过 git ls-files 排除未跟踪且被忽略的产物，仍检查已跟踪文件。

## 故障

- 找不到 Python、Git、zstd 或 Codex CLI：报告缺失项，让用户按其机器环境安装；不自动下载未知脚本。
- Codex 提示未登录：请用户完成 `codex login`。不读取、粘贴或打印认证文件。
- 模型被服务端拒绝：查看该次 `reviews/<request-id>/stderr.log`；选择本机可用模型，以 `start --model ...` 覆盖。不要未经请求修改全局配置。
- 端口占用：查看 `server.log`；用一致的 `--port` 参数选择空闲端口。
- 审查器忙：等待最多 30 秒后返回 blocked；保持暂停，稍后使用同一 request_id 重试。
- 超时或每日上限：返回 blocked，停止任务并报告，不自动放行。

事件与结果位于状态目录中的 `events.jsonl`、`reviews/<request-id>/request.json`、`result.json`、`before.json`、`after.json`；模型执行痕迹位于 trace.jsonl。请求中只发送本任务的必要资料。

仅监听 127.0.0.1；请求使用状态目录中的本机令牌认证。服务无轮询模型调用，审查采用 Codex workspace-write 沙箱，审查提示词要求保留实现文件、只检查和返回指令。实现文件变化被文件证据记录，skill 的暂停保证以独占前台交接且无后台开发为前提。

## 管理看板（新版）

本仓库的 React 看板默认在 `127.0.0.1:13084`，运行方法见根目录 README。
新版桥接依赖同目录 `review_core.py`；安装 skill 时复制完整目录。
看板和桥接通过相同状态目录的 SQLite 共享审批模式、审查状态和操作记录。
已有旧服务不会自动升级或被看板停止，应先通过原 CLI 停止，再启动新版。

新版总交接期限为 540 秒，包含排队、审查和人工审批；原“忙时等待 30 秒”的规则由此取代。
相同 request_id 始终复用原结果，`blocked` 结果也不会重跑。需再次阻塞审查时使用新的 request_id。
页面手动审查及重试属于观察模式，不会批准或恢复 DeepSeek。人工审批只用于仍在等待的阻塞交接。
管理事件保存在 dashboard.sqlite3；旧 events.jsonl 在看板中仍可查看。
