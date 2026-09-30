# DeepSeek ↔ GPT 监工工作台

React + Vite + TypeScript 管理看板，配套 Python 本地管理服务、阻塞式审查桥和 Harness 连接插件。所有指标来自真实会话、审查记录和服务状态；空数据不会用示例数据替代。

## 启动看板

需要 Node.js 22.12+、Python 3.10+。执行真实审查还需要 Git、zstd，以及已登录的 Codex CLI。

```sh
npm install
npm run build
npm start
```

访问 **http://127.0.0.1:13084**。看板默认读取 `$DSH_HOME`（未设置时为 `~/.dsh`），状态保存在 `$DSH_SUPERVISOR_STATE`（未设置时为会话目录下的 `supervisor`）。首次启动只运行管理服务；在总览页点击“启动监工”才启动审查桥。

开发时开两个终端：

```sh
# 终端一：管理 API
npm start
# 终端二：Vite，访问 http://127.0.0.1:5173
npm run dev
```

自定义管理端口或独立状态目录：

```sh
python3 -B dashboard_server.py --port 13084 --state-dir /绝对路径/supervisor-state
```

设置页可修改模型、超时、额度、会话目录和历史审查目录。模型、超时、额度只影响后续审查；更改会话目录或桥接端口前先停止监工。历史目录是含有 `reviews/`、`events.jsonl` 的上一级目录。它仅用于读取历史，当前服务不会迁移或改写旧目录。

### 已有旧版服务

看板能识别旧服务并读取历史记录，但旧服务没有人工审批和取消接口。请在原服务空闲时，使用原来的 CLI 与相同状态目录停止旧服务，再从看板启动新版。看板不会自动终止旧服务，也不会清零 `quota.json`。

```sh
python3 -B dsh-gpt-supervisor/scripts/bridge.py stop --state-dir /原状态目录
```

同一个状态目录只运行一个桥接服务；不要同时启动旧版 `dsh_supervisor.py` 单向监工。

## 五个页面

- **总览**：服务启停、会话数量、审查进度、人工待办、每日额度和近期异常。
- **会话管理**：搜索会话、查看工作区和最近事件、设置后续交接的人工审批模式、即时插话、停止当前轮次。子代理只读。
- **审查中心**：阶段与状态筛选、观察审查、取消、重试、人工接管、阶段批准/要求修改、最终验收通过、暂停证据与执行日志。
- **操作日志**：检索服务、审查、配置与消息事件，跳转关联记录；同时展示选定历史目录的事件。
- **设置**：保存后续审查参数，检查 Harness 插件连接和目录匹配情况。

“手动审查”和页面“重新观察审查”不会暂停或恢复 DeepSeek。若要恢复被阻塞的协作任务，应在 DeepSeek 端发起新的前台交接，使用新的 request ID。相同 request ID 的重复交接返回原有结果，包括旧版缓存；不能通过重试改写历史结论或绕过额度。

## 安装 Harness 连接插件

插件使用 Harness Host 的 `apiProxy.sessions.prompt`（`steer`）和 `cancel`，不依赖浏览器 Cookie。接口按本机已安装的 Harness `0.1.1-rc.2` 契约实现。

使用 `sessionController` 的新版 Harness（本机源码版本 `0.1.7-rc.1`）需要在 profile 的 `cordis.patch.yml` 中停用旧入口并插入新版入口：

```yaml
- id: dsh-supervisor-connector
  name: '@dsh-supervisor/connector'
  disabled: true
- insert:
    - id: dsh-supervisor-connector-modern
      name: '@dsh-supervisor/connector/session-controller'
```

该入口把操作 ID 传入 `prompt.requestId`，通过只读 `inspect` 核对消息是否被消费，并沿用原有连接心跳和送达记录。不要在缺少 `apiProxy` 的新版宿主中使用默认入口，否则插件会等待不存在的依赖，无法连接。宿主调用抛出异常时保守标为“送达待核实”，不自动重发。

1. 执行 `npm run build`。
2. 在**目标 Harness 实例**使用的 CLI、DSH_HOME 和 profile 下安装本仓库的 `connector` 包。确保该 CLI 的 PATH 中有 pnpm。例如将下列 `你的profile` 替换为实际 profile：

   ```sh
   dsh plugin --profile 你的profile add /绝对路径/dsh-gpt-tools/connector
   ```

3. 加载或重启该 profile。到看板“设置”检查“插件在线”和“目录匹配”。不同桌面实例可能使用不同的 DSH_HOME；不要仅凭 CLI 名称判断实例。

插件默认连接 `http://127.0.0.1:13084`，使用 `$DSH_SUPERVISOR_STATE/connector.key`（未设置时为 `$DSH_HOME/supervisor/connector.key`）。管理服务首次启动生成该文件，权限为 600；无需把令牌复制进网页。

若管理状态与 Harness home 不同，在插件的 profile 配置项下指定：

```yaml
- id: dsh-supervisor-connector
  name: '@dsh-supervisor/connector'
  config:
    stateDir: /绝对路径/supervisor-state
    home: /实际/Harness/home
    origin: http://127.0.0.1:13084
```

该配置应替换已有插件项的配置，避免重复加载插件。停止当前轮次会使用 Harness 自身的取消语义：正在执行的工具可能被中断，已有排队消息会保留，之后仍可能启动新轮次。

### 指令送达含义

- **提交中 / 正在送达**：管理服务已记录操作，等待插件领取或回执。
- **Harness 已接收**：会话 API 已接受，尚不能据此断言模型执行完成。
- **会话已消费**：会话历史中的 `user/message` 通过源 `rpcId` 与指令精确匹配。
- **失败**：插件未及时领取，或 Harness 明确拒绝。
- **送达待核实**：调用/回执中断，结果不确定；不会自动重发相同输入。请先查看目标会话。

正在等待交接时提交人工指令，会取消旧审查，等待其 `blocked` 响应返回并停止审查进程后，再发送插话。若原交接响应未确认返回，页面会报错，指令不会发送。

## 阻塞式交接 CLI

新版通用桥默认端口 **13083**：

```sh
python3 -B dsh-gpt-supervisor/scripts/bridge.py start
python3 -B dsh-gpt-supervisor/scripts/bridge.py status
python3 -B dsh-gpt-supervisor/scripts/bridge.py stop
```

在 DeepSeek 的当前项目根目录，以唯一前台工具调用交接：

```sh
python3 -B /绝对路径/dsh-gpt-tools/dsh-gpt-supervisor/scripts/bridge.py handoff \
  --phase plan --summary-file /绝对路径/plan.md --scope src
```

阶段支持 `plan`、`checkpoint`、`acceptance`；`--scope` 可重复，默认整个项目。Harness 提供 `DSH_SESSION_ID`，其他调用方可显式传 `--session-id`。使用 `timeoutMs=600000`、`run_in_background=false`；等待期间不得并行开发或启动后台任务。

默认每次模型审查 240 秒，每日 12 次，按 Asia/Shanghai 日期计数。排队、模型审查和人工审批合计最多 540 秒。取消、超时、服务退出、暂停证据失败均返回 `blocked`。人工审批提交时会再次核对会话活动和源文件哈希。

根目录 `dsh_bridge.py` 保留原 **13081** 端口与 `../work/dsh-bridge` 状态目录，作为共享实现的兼容入口。

## 安装与使用 Skills

提供两个管理端 skill，可分别复制整个目录到本机 `~/.codex/skills/`：

| Skill | 源码目录 | 用途 |
| --- | --- | --- |
| `dsh-supervisor-install` | `skills/dsh-supervisor-install` | 安装或升级看板，配置桥接服务与 Harness 连接插件，核对连接 |
| `dsh-supervisor-use` | `skills/dsh-supervisor-use` | 查看真实状态、管理人工审批、按要求发送指令或停止会话 |

调用示例：

```text
使用 $dsh-supervisor-install 安装 /绝对路径/dsh-gpt-tools，连接我指定的 Harness 实例。
使用 $dsh-supervisor-use 查看服务状态，列出等待人工审批的审查。
```

使用 skill 自带 Python 标准库 API 客户端，自动处理本地 Cookie/CSRF。写操作需明确指定操作 ID；网络异常不会自动重复发送。安装 skill 需要项目源码路径，不会将 skill 目录当成完整项目。原 `dsh-gpt-supervisor` 仍用于 DeepSeek 任务内部的阻塞交接。

修改 skill 后运行 `npm run sync-skills` 同步仓库 `.agent/skills/` 分发副本。

## 代码结构与验证

```text
dashboard/                         React 页面与样式
connector/                         Harness Host 连接插件
dashboard_server.py                管理 API、进程管理、静态页面托管
dsh-gpt-supervisor/scripts/
  review_core.py                   SQLite、会话解析、审查状态控制与模型执行
  bridge.py                        原 CLI 和阻塞交接 HTTP 入口
scripts/sync-skills.py              同步 .agent/skills 下的分发副本
tests/                             后端、插件与浏览器集成验证
```

```sh
npm run typecheck
npm test
npx playwright install chromium
npm run test:e2e
npm run sync-skills
```

浏览器集成测试使用临时工作区、确定性本地审查程序和 Harness API 测试替身。它启动真实管理 HTTP 服务、桥接子进程和实际连接插件，验证前端操作及回执；不会调用真实模型或向已有会话发送测试消息。截图保存在 `.playwright/`。与真实 Harness 的最终连接状态以设置页和实际会话事件为准。

修改共享桥接实现后执行 `npm run sync-skills`；skill 安装时应复制整个目录，包含 `review_core.py`。构建产物和运行状态不提交到 Git。

管理 API 只监听回环地址，网页使用 HttpOnly/SameSite Cookie、CSRF 令牌和来源校验；插件使用独立本机令牌。数据库保存审查与操作状态，模型输出与原始证据保存在 `reviews/<request-id>/`。日志详情展示末尾 24 KB，完整日志保留在磁盘。
