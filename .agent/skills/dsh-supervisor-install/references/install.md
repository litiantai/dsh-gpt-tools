# 安装与升级流程

以下 `<project>`、`<home>`、`<state>`、`<profile>` 均替换为已核实的实际值；所有含空格的路径加引号。

## 1. 构建并运行看板

在项目根目录：

```sh
npm ci
npm run build
DSH_HOME="<home>" python3 -B "<project>/dashboard_server.py" --state-dir "<state>" --port 13084
```

默认访问 `http://127.0.0.1:13084`。需要长期后台运行时，用参数数组创建后台进程并重定向到已知日志文件，记录 PID；不要重复启动，不自行安装开机启动项。首次启动管理服务会创建私有状态目录与 connector.key，不会自动启动模型审查。

开发模式额外运行 `npm run dev`，Vite 使用 5173，默认代理到管理端口 13084。更改管理端口后相应调整代理；正常交付使用构建后的静态页面。

## 2. 审查桥与旧版切换

在设置页确认模型、`home`、`bridge_port`。总览页启动监工会使用保存的配置。直接 CLI 启动时显式传入需要保留的配置，避免默认值覆盖已保存设置：

```sh
python3 -B "<project>/dsh-gpt-supervisor/scripts/bridge.py" start \
  --state-dir "<state>" --home "<home>" --port 13083 \
  --model "<已确认的模型>" --review-timeout 240 --max-per-day 12
```

旧版 `GET /health` 通常只有 `ok` 和 `mode`；新版另有 `protocol: dashboard-v1` 与 `pid`。仅凭 `ok=true` 不能判定兼容。

确认旧实例空闲、PID 对应确切脚本与状态目录，且用户要求切换后，使用原 CLI 停止，再启动新版。原 `dsh_bridge.py` 默认 13081 / 项目上一级的 `work/dsh-bridge`；新版通用桥默认 13083 / `<home>/supervisor`，不要混用。

保留 `quota.json`、`reviews/`、`events.jsonl` 和 `dashboard.sqlite3`。设置页 `history_dir` 仅控制历史读取，不是迁移或当前状态目录设置。

## 3. Harness Host 插件

先核对 `dsh` 的真实启动器、DSH_HOME 与 profile；有的桌面启动器会自行覆盖 DSH_HOME，给命令设置同名环境变量并不能改变它。

在正确实例上执行：

```sh
dsh plugin --profile "<profile>" add "<project>/connector"
```

安装前检查是否已存在 `@dsh-supervisor/connector`，已有时更新该项，避免重复加载。该命令依赖 pnpm；不要把插件装到另一个桌面实例来绕过当前实例的问题。

若目录或端口非默认，在已有插件项设置：

```yaml
- id: dsh-supervisor-connector
  name: '@dsh-supervisor/connector/session-controller'
  config:
    stateDir: /absolute/supervisor-state
    home: /absolute/harness-home
    origin: http://127.0.0.1:13084
```

加载 profile 后，以看板设置页报告的 home 与心跳为准。`connector.key` 由管理服务保存在 state 中，插件自行读取，不复制进网页、聊天或日志。

当前 bundle 已插入新版入口，不要再手动插入 `dsh-supervisor-connector-modern`。升级时删除旧的额外插入项并把配置移回默认项；patch 的 `name` 是匹配条件，旧入口名称不会匹配新版入口。旧宿主必须用无 `name` 的 ID override 停用默认项，再只插入一个旧版连接入口。

## 4. 最终核对

- 静态页面可打开，`/api/bootstrap` 能建立本地会话，`/api/overview` 可读。
- 审查桥运行且兼容；旧版仍运行时明确标为待切换。
- 插件在线且目录匹配；“在线但目录不匹配”仍不能发送指令。
- 原始额度、历史审查文件存在；只执行只读连接检查不消耗模型额度。
- 使用说明中给出实际状态目录，避免后续 CLI 与看板操作不同实例。
