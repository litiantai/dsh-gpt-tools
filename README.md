# DeepSeek ↔ GPT 监工工作台

## 通用仓库接入与自进化

点击首页的「接入仓库」，提供本地 Git 目录或 HTTPS Git URL。平台在独立工作区记录技术栈、命令来源、依赖、源码提交、构建与测试结果、启动健康、截图及停止结果。启动验证与业务验收分开记录；缺少启动入口、隔离依赖或凭据时显示阻塞，不生成成功证据。重试建立关联的新扫描，原记录不变。通过后可登记为观察模式，或通过版本校验将配置应用到同一项目。

项目可通过 `.autopilot.json` 声明版本 1 配置：`commands` 中的 `install/build/test/browser/start` 均为命令参数数组列表，启动命令支持 `{port}` 与 `{state}`。`readonly_paths` 限定正式实例只读断言，`acceptance_checks` 提供命名的隔离测试（探针路径为 `check:名称`）。`artifacts` 声明产物目录，`exclude` 排除不属于发布基线的材料。仓库内本项目配置是完整示例。

安装依赖禁用生命周期脚本；执行阶段禁止外部网络，测试只能访问配置的本机端口。`test-loopback` 用于需要随机端口的集成测试，并拒绝访问平台正式端口 13081/13083/13084。文件读取隔离用户目录、应用数据及其他临时工作区；运行目录、临时数据与正式状态分离。当前隔离后端为 macOS Seatbelt，其他系统明确阻塞。Python/JVM 项目会记录识别结果，未提供安全安装和启动配置时等待补充，不套用 Node 命令。

```sh
# 扫描或登记新项目；不再默认接入旧桌面产品。
python3 -B scripts/autopilot.py scan --source /绝对路径/仓库
python3 -B scripts/autopilot.py onboard --source /绝对路径/仓库
# 从固定版本的公开软件包建立独立执行运行时。
python3 -B scripts/autopilot.py install-runtime --runtime-version 0.1.7-rc.1
# 兼容迁移保留旧项目 ID、字段及证据；重复执行不会重复转换。
python3 -B scripts/autopilot.py migrate-records
```

新增 `GET/POST /api/scans`、`GET /api/scans/<id>`、`POST /api/scans/<id>/retry|onboard|apply`；写入沿用 Cookie、CSRF、操作 UUID 与版本控制。应用扫描结果额外核对项目版本。项目配置包含 `schema_version`、`adapter_spec`（协议版本、类型和能力）、`project_config` 及扫描引用；不支持的能力明确阻塞。`automation_disabled` 停止该项目的后台工作，历史记录只读保留，不转入其他项目。

`dsh-gpt-tools` 使用独立版本目录运行，稳定更新器位于状态目录的 `platform/updater`，不会随普通候选版本覆盖。首次用 `scripts/install-platform.py --state-dir … --manifest …` 准备已验证基线与用户级服务。后续更新经 Git 交付、候选校验后排队；独立更新器停止派发、等待执行进程退出、备份数据库、切换版本、检查 API 与调度心跳，并完成健康观察。只允许向后兼容的数据迁移；失败回滚代码并保留新增历史，不用旧数据库覆盖新记录。更新日志保存于 `autopilot/updates`。

## 空闲时段运行

项目设置 → 运行策略中的“DeepSeek 仅空闲时段运行”默认开启（旧项目未配置时也按开启处理），可在任务执行中调整。开启后，本项目使用 DeepSeek 官方供应商的研发、调查、业务验证、方案/验收审查、日报及代码交付评审/修复任务，在北京时间周一至周五 09:00–12:00、14:00–18:00 排队，空闲时段自动继续；周末（包括调休上班日）及中国节假日全天放行。Codex、Claude 和其他供应商不受影响，未显式配置供应商的旧 Harness 任务保守应用限制。等待保存在台账中，重启后继续，不消耗调用超时、需求发现额度或修复轮数；关闭开关可解除时段等待。开关控制执行任务的启动时间，已启动的多轮 Agent 任务允许完成，因此不保证跨越高峰边界后每一笔底层 API 请求都享受优惠。手动会话及平台独立观察审查不属于项目调度范围。

计费规则核对于 2026/10/09，来源为 [DeepSeek 官方价格说明](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。节假日表保存在 `autopilot/off_peak.py`，已按 [2026 年国务院放假通知](https://www.gov.cn/zhengce/content/202511/content_7047090.htm) 收录全年放假日期；新增年度需更新该表。未收录年份在页面提示，并保守按普通周一至周五高峰时段排队，周末仍正常放行。


## 持续研发与项目链路概览

首页 `/autopilot` 以项目为单位展示巡检信号、需求池、任务规划、方案评审、开发、测试、验收、待发布及上线观察，并展示返修与异常回路。支持节点明细、项目切换、画布拖动、缩放与全屏。原有会话、审查和日志按源码目录及受管任务工作区关联到项目；原监工总览保留在 `/supervisor`。

项目工作区按使用流程分为「工作概览、需求池、任务流水线、巡查与证据、发布记录、项目设置」六个主标签。会话、审查和日志集中在任务流水线；巡查步骤、结果、判断、截图及实践评测集中在巡查与证据。主标签与子标签保存在 URL 中，切换页面、刷新、浏览器返回都能恢复当前位置；从平台管理返回时保留最近打开的项目。列表支持按名称、原因、编号搜索及按状态筛选。

项目设置包含模型分工、运行策略和接入环境。时间间隔使用分钟或小时展示，保存时转换为后台的秒数；切换配置子标签保留修改草稿，支持放弃修改。运行模式与配置保存分开操作，有执行中的任务时配置保持只读。侧栏「平台管理」中的会话、审查、日志和设置作用于全部项目。

新增 `autopilot/` 提供 SQLite 任务台账、去重与额度、持久化执行回执、独立模型执行器、通用命令项目适配器及旧桌面产品兼容适配器。调度器与管理网页分开运行。新接入项目默认每分钟探测、每小时巡检、每天最多五项开发、开发 Token 上限一千万；已有项目保留原策略，审查仍使用原有额度。开发执行器在 macOS 写隔离内运行，禁止访问正式应用数据和本机管理 HTTP 接口。

```sh
# 建立独立源码基线、固定执行运行时，登记为观察模式；不改原工作树。
python3 -B scripts/autopilot.py onboard
# 注册当前用户的 launchd 常驻服务。
python3 -B scripts/autopilot.py install-service
python3 -B scripts/autopilot.py status
```

接入状态保存在 `<state>/autopilot/`，与原审查共用 `dashboard.sqlite3`，但不改变原监工的 home 配置。`onboard` 不会自动替换正式桌面应用。产品配置中可切换观察、暂停及自主运行。启用前需完成项目扫描、模型配置、隔离依赖、业务验收与部署能力检查；缺失项会保留真实阻塞记录。

新接口：`/api/products`、`/api/signals`、`/api/requirements`、`/api/runs`、`/api/releases`、`/api/workers` 和 `/api/autopilot/metrics`；`/api/products/<id>/workbench` 返回该项目的会话、审查及事件。写操作沿用 Cookie、CSRF、操作 UUID 和记录版本检查。

持续运行可在项目设置中配置每日需求处理上限和每日 Token 上限，0 表示不限。额度按北京时间每日零点恢复；达到上限后保留队列及阶段进度，额度提高后自动继续。开发 Token 上限仅覆盖需求方案、方案审查、开发、业务验证和业务验收；需求发现与前期调查使用 `discovery_tokens` 独立账本，不受该 Token 上限限制。Token 从 Codex/Harness 与业务审查原始回执增量入账，缓存输入计入、重复轮询不重复扣减；已开始的调用不被强制中断，因此实际用量可能超过上限，超限后停止后续模型调用。平台审查次数仍独立约束审查派发。

项目页面在今日开发 Token 额度耗尽时显示提醒和“调整今日上限”。临时上限只对北京时间当天生效，保存后调度器自动重新判断，次日零点恢复项目默认上限；0 表示今日不限。调整不清零用量，代码复审等独立账本不占开发额度。

定时巡检不占需求和 Token 额度。以下桌面实例流程仅用于历史兼容适配器；通用项目采用上述命令配置与独立更新器。启用 Git 交付后，巡检与合入后的最终验收共用 master 主实例，先核对远端主分支、真实 Host 进程和运行版本；当前主实例巡检检查只读状态、自选和市场接口，不把接口通过标记为完整界面业务验收。开发和业务验收共用一个串行占用的独立测试实例，测试过程保留界面截图；历史候选产物保留为证据，不再作为 Git 项目的巡检实例。合入 master 后，只有内容与 master 完全相同且验收产物校验通过的 release 构建可用于更新主实例；等待连续空闲后原位更新，失败沿用发布回滚。最终验收在主实例逐项执行原需求的只读效果断言，缺少断言、版本不一致或实例不可用均不得通过。代码合入、客户端更新、最终验收分别保存状态，晚间复验只针对已合入 master 的需求。未启用 Git 交付的项目保留原有隔离巡检路径。完成限定数量评测后，可通过“切换持续运行”解除该批次的数量限制，历史评测保留；已验收任务可单独“进入发布”，仍需候选校验、连续空闲、健康观察与回滚保护。

发布使用候选产物清单、完整运行时和桌面应用，切换日志保存至 `deployments/<release-id>/journal.json`。切换前重新核对空闲状态；保留用户 profile 补丁和数据，失败时恢复旧运行时及应用。跳过必需测试、缺少原需求的效果证据和未知副作用均不视为成功。暂停、重启、睡眠后的检查间隔过长会重置连续空闲或健康观察时间。

验证命令为 `npm test`、`npm run typecheck` 和 `npm run test:e2e`。新增测试使用临时源码仓库与确定性模型替身，涵盖完整状态链路、并发去重、回执恢复、空闲发布、产物篡改、发布中断与失败回滚。它们不代表真实产品的 72 小时试运行已完成；持续试运行需单独核对实际发布和问题修复证据。

React + Vite + TypeScript 管理看板，配套 Python 本地管理服务、阻塞式审查桥和 Harness 连接插件。所有指标来自真实会话、审查记录和服务状态；空数据不会用示例数据替代。

## 启动看板

需要 Node.js 22.12+、Python 3.10+。执行真实审查还需要 Git、zstd，以及已登录的 Codex CLI。

```sh
npm install
npm run build
npm start
```

访问 **http://127.0.0.1:13084**。看板默认读取 `$DSH_HOME`（未设置时为 `~/.dsh`），状态保存在 `$DSH_SUPERVISOR_STATE`（未设置时为会话目录下的 `supervisor`）。管理服务提供看板和审查记录；新版连接插件启用时会自动确保审查桥运行，也可在总览页手动启停。

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

## 插件自动强制监管

Harness `0.1.7-rc.1` 的 `@dsh-supervisor/connector/session-controller` 入口默认启用强制监管：

1. 新建或继续执行的主会话在步骤边界自动接管；历史闲置会话不补扫。
2. DeepSeek 先输出方案，方案审批前所有工具被限制。插件自动提交 GPT，批准后继续开发。
3. 最近 5 次工具结果出现至少 3 次明确失败时暂停检查；完成回答后、轮次结束前自动验收。回答可能已经流式显示，此时以看板“等待 GPT 审批 / 验收”为准。
4. 返修意见自动进入原会话，每个阶段最多连续返修 3 次。子代理由主会话统一审查；后台任务和子代理仍活动时暂停审查，结束后自动提交；如果审查期间出现新的后台活动，阻塞并要求重试。
5. 服务不可用、超时、额度耗尽或暂停证据失败都不会自动放行。会话详情及审查详情提供“重试监管审查”和填写原因后的“仅放行当前审批点”。人工放行不改变 GPT 原结论或暂停证据。

明确失败包括工具的 `isError`、Bash 非零退出标记，以及 Bash 输出中的 Vitest、TAP、Python unittest 失败汇总。这样 `测试命令 | tail` 即使返回退出码 0，已显示的失败汇总仍会计入；普通文字里的 “error” 不作为失败依据。

监管启用与审批方式是两件事：“自动”表示 GPT 直接审批，“人工”表示等待人工提交结论。停止服务后插件保持阻塞；显式重试会重新启动服务。取消任务或卸载插件会取消等待，迟到结果不能恢复任务。新用户指令重新触发方案审批；插件恢复指令不重复触发。

插件使用原生 `agent/pre-step`、`agent/turn-stopping`、工具单调 guard 及作用域工具限制；不需要加载 `dsh-gpt-supervisor` skill。仅安装 skill 的旧流程仍需主动 CLI 交接，不要在已自动监管的会话重复发起 CLI 交接。

默认每天 12 次审查、单次模型 240 秒、一次审查排队及审批总计 540 秒；故障后的插件等待不自动放行。审批点心跳租约为 15 秒，重连与重启均沿用原 ID 核对结果，断线后的未消费结果需重新审查。

### 原生插件接口

- `POST /api/connector/native`：使用现有 `connector.key` Bearer 认证，并核对 `home`。`action` 为 `enable/state/open/poll/hold/cancel/ack`。每个审批点有固定 UUID 和持有者 UUID；`open` 携带阶段、摘要、真实暂停事件序号及后台静止状态。
- `GET /api/native-gates/<id>`：浏览器登录会话读取审批点、审查结果、心跳和人工放行记录。
- `POST /api/native-gates/<id>/retry`：浏览器 Cookie + CSRF + 操作 UUID，传当前 `review_id`（未创建审查时为 null）。创建新的审查尝试，保留旧结果。
- `POST /api/native-gates/<id>/release`：同上，并必填 `reason`；仅作用于在线、后台已停止的当前阻塞审批点。记录本机管理会话身份、时间、原因与原审查 ID。

`native_handoff` 与原 `handoff` 共用审查引擎及暂停验证。`observation` 仍只观察。数据库新增 `native_gates/native_sessions`，插件恢复记录位于 `<state>/native-connector/`；升级和回退都保留原数据库、额度及审查目录。

### 测试

`npm test` 包含真实已安装 Harness 的独立内存会话测试，使用脚本化模型响应，不发送真实用户会话消息或消耗 GPT 额度。可用 `DSH_RUNTIME_NODE_MODULES` 指定运行时 node_modules；默认查找独立的 `~/.dsh/supervisor/autopilot/runtime`，缺失时该项明确跳过。`npm run test:e2e` 验证本机隔离看板，包括阻塞和人工放行。

## 五个页面

- **总览**：服务启停、会话数量、审查进度、人工待办、每日额度和近期异常。
- **会话管理**：搜索会话、查看工作区和最近事件、设置后续交接的人工审批模式、即时插话、停止当前轮次。子代理只读。
- **审查中心**：阶段与状态筛选、观察审查、取消、重试、人工接管、阶段批准/要求修改、最终验收通过、暂停证据与执行日志。
- **操作日志**：检索服务、审查、配置与消息事件，跳转关联记录；同时展示选定历史目录的事件。
- **设置**：保存后续审查参数，检查 Harness 插件连接和目录匹配情况。

“手动审查”和页面“重新观察审查”不会暂停或恢复 DeepSeek。若要恢复被阻塞的协作任务，应在 DeepSeek 端发起新的前台交接，使用新的 request ID。相同 request ID 的重复交接返回原有结果，包括旧版缓存；不能通过重试改写历史结论或绕过额度。

## 安装 Harness 连接插件

旧版入口使用 Harness `0.1.1-rc.2` 的 `apiProxy.sessions.prompt`（`steer`）和 `cancel`，只提供连接与指令送达，不支持强制监管。新版入口使用下述生命周期能力。

当前 bundle 默认加载 `sessionController` 新入口（Harness `0.1.7-rc.1`）。profile 只需覆盖已有项，不要另插入第二个监管入口：

```yaml
- id: dsh-supervisor-connector
  name: '@dsh-supervisor/connector/session-controller'
  disabled: false
```

升级前若曾手动插入 `dsh-supervisor-connector-modern`，先删除该插入项并把配置移回上面的默认项。`name` 是匹配条件，不是更换入口的指令；旧名字的 `disabled: true` 无法停用新版入口，会导致重复审查。旧宿主需停用默认项（只写 `id` 和 `disabled: true`），再单独插入 `@dsh-supervisor/connector` 连接入口。

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
  name: '@dsh-supervisor/connector/session-controller'
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

## 多审查器与模型

设置页支持“统一配置”和“按阶段配置”。每组通过下拉选择审查器（Codex/GPT、Claude CLI、DeepSeek Harness）与模型；首次切换分阶段时从统一配置初始化，之后两套选择独立保留。保存仅影响后续新审查；已经入队的请求持久化审查器、模型、路径和超时快照。

模型目录直接来自本机工具：Codex app-server 的 `model/list`、连接指定 Claude CLI 的 Agent SDK `supportedModels()`、独立 Harness profile 的运行时 `listProviders()/listModels()`。列表刷新不发送用户任务、不计审查额度；5 分钟内复用缓存，失败显示错误与上一次目录时间。Claude 的别名可能映射到自定义供应商模型，以 CLI 实际返回名称为准。模型出现在目录不保证账户调用额度；执行失败保持阻塞，不自动换审查器。

接口：`GET /api/reviewers/{codex|claude|harness}/models`，`POST /api/reviewers/{provider}/models/refresh`；后者沿用 Cookie、CSRF、操作 UUID，可传未保存的 `codex_bin/claude_bin/harness_bin/harness_profile` 预览目录。`/settings` 新增 `reviewer_mode`（`unified/stages`）、`reviewer_unified`、`reviewer_stages`（`plan/checkpoint/acceptance`）；每个选择含 `provider/model`，Harness 另含 `model_provider`。审查详情的 `reviewer` 保存实际选择；旧记录没有模型时显示“未记录”。

Harness 必须使用独立的 base + headless profile，不能选择 web/desktop/tui；审查进程不启动连接插件、不消费开发会话，日志存入本次审查目录。可用下面的安装脚本创建专用 profile（目标已存在时拒绝覆盖）：

```sh
node scripts/setup-reviewer-profile.mjs /实际/Harness/home web supervisor-review /实际/Harness源码或安装根目录
```

脚本保留源 profile 的模型配置覆盖，复用已有凭据；不复制凭据文件。高级设置的 Harness CLI 必须指向与该 home 匹配的实际启动器。桌面生成的 `dsh` shim 可能强制覆盖 `DSH_HOME`，不能直接用于另一个实例。目录发现和真实调用使用同一个 profile。

CLI 运行环境需要 Node.js、已安装依赖及各工具现有认证。更新部署时复制完整 `dsh-gpt-supervisor/scripts`，保留已保存的 `reviewer_helper`（指向本仓库并可解析 Agent SDK）及独立 profile。三种适配器统一检查阶段结论、超时、取消、暂停证据和人工审批；查询目录不触发收费验收。

独立真实调用验证（会产生三个模型调用，需明确联调授权）：

```sh
python3 tests/live_reviewers.py --run --home /实际/Harness/home --state-dir /独立测试状态目录 --harness-bin /实际/Harness启动器
```

它创建明确标注的测试夹具会话，以原交接模式验证三种审查器的真实文件读取、测试和验收；自动插件阶段拦截由 `connector/test/native-harness.test.mjs` 的真实 Harness + 脚本化模型测试覆盖。

### 每日研发日报与复验

项目的“巡查与证据 → 日报与复盘”可开启每天北京时间 19:00 的日报。调度器先保存报告快照，再串行复验统计时段内已验收的候选版本，最后由独立评估模型输出中文进展、问题、复盘与后续行动。首次统计当天零点至 19:00，之后从上次截止时刻至本次 19:00；晚间验收不会漏掉。离线恢复后补生成最近到期报告，并从上次报告截止时刻汇总。已启动的模型调用先结束，复验期间继续巡检。

日报、复盘与晚间复验的模型用量记入 `daily_report_tokens` 独立账本，不占需求数量、开发 Token 和常规审查次数。复验核对原候选提交、源码与产物完整性，并重新执行针对性检查；失败或无法验证会明确显示，原验收和发布记录保留。每个调用有持久化回执，服务重启后继续尚未完成的步骤。电脑或服务停机期间不能实际执行，到期报告会在恢复后补跑。

任务详情、执行回执、巡查证据和日志详情默认显示中文字段与检查结果；完整技术数据保留在折叠的“原始数据（排查用）”中。

启用项目的晚间归因模式后，体验巡检每小时一次，白天仅采集证据。北京时间 19:00 在日报流程中先统一归因：覆盖本统计时段内的全部巡检（含失败巡检）及历史待处理信号，分批处理直到全部覆盖。每条信号保留中文归因结论；同一信号可以对应多个需求；重复问题合并，证据不足转为调查需求。新增需求标记报告日期并列入当日需求清单，开发需求按原每日数量与 Token 上限排队。晚间归因沿用日报独立用量账本。归因失败的信号保留到下次晚间处理，不记为已完成。19:00 后产生的巡检归入下一次晚间统计。

### 调查与故障恢复

需求池中的调查及环境需求会串行进入“调查取证中”，执行只读源码检查、针对性验证和隔离环境取证。有实际检查记录、可执行验收条件和合法的只读效果探针后，同一需求转入开发队列；无法解决的外部条件会明确展示原因及下次自动复查时间（24 小时后）。每次当日调查占用一个需求名额，Token 计入 `discovery_tokens` 独立账本，不占开发 Token 额度，同一需求由调查转开发不会重复扣名额；单纯等待不启动模型。

隔离任务的审查超时或进程退出故障，在确认原审查进程及子命令已经停止后，可分别间隔 1、5、15 分钟自动重新提交，最多三次。旧结论保留，新请求重新核验暂停证据；人工阻塞、停止、未确认副作用和验收失败不会自动放行。隔离任务审查执行时间至少 10 分钟，等待窗口至少比执行时间长 3 分钟；其他交接客户端的超时不变。其他审查正在运行时，等待调度而不提前消耗交接等待时间。

上线观察遇到瞬时网络超时会清空连续健康计时并重试；连续三次仍无法完成观察才触发回滚。版本不一致或原问题效果断言失败仍立即按失败处理。

### 每日 Git 交付与模型评审

在 **项目设置 → Git 与代码评审** 中配置 GitHub 仓库地址、目标分支、代码评审模型、问题修复模型及自动修复轮数。代码交付默认不限修复轮数（`max_revisions=0`）；显式设置 1–20 时限制每批次每天新启动的修复轮数，用尽后排队至北京时间次日零点自动继续；累计历史轮数不再永久阻塞批次。评审、验证和合并不消耗修复额度，修复后仍须重新评审和验证。评审和修复分别支持已有的 Codex、Claude、Harness 执行器，默认继承业务验收和实现角色；每轮启动时冻结模型与推理配置，后续修改不改写历史轮次。目录读取失败不会自动替换已选模型。

业务验收通过后，每个需求的增量进入其固定 `feat-<需求标识>` 分支，并绑定唯一一个 `feat → release` 代码评审 PR；返修与重试复用原 PR，跨日延期将原 PR 的目标切换到次日 release 并重新评审，不为同一需求重复建 PR。每次只准备、评审并合入一个需求，其他需求仍排队。`release-YYYYMMDD` 每天唯一、从 `master` 创建，代码评审通过前禁止向其合入成果；评审失败记录问题，独立修复后重新评审。该阶段只使用固定 OCR 规则与用户所选模型逐文件 Code Review，不启动客户端。每个评审 MR 地址、提交和实际模型分别保存；同日可有多个已合并的评审 MR，仍共用一个 release 和一个 `release → master` 上线 PR。

成果真正合入 release 后才标记“已交付未上线”。北京时间 **23:30 封板**，不再开启新的代码评审；未合入成果及修复补丁保留到次日重新评审。封板后的 release 只针对实际已合入成果执行必需检查和业务回归，不带入延期任务的验收条件，也不逐个成果启动客户端验证。统一验证通过后优先按日期顺序合入 master；仅因修复额度排队的批次不占住后续合格成果的上线队列。目标分支或 release 变化会使证据失效，冲突和修复需重新评审，23:30 后的复审等待次日窗口。只有 GitHub 确认上线 PR 已合并并返回 merge SHA，关联需求才显示“已上线”；客户端安装和回滚独立记录。

首次接入先保存配置，再点击 **建立首次基线 PR**。服务检查本机 GitHub 认证的推送权限和 merge commit 支持；可在 **Git 与代码评审 → 本机 GitHub Token** 直接输入并验证保存。Token 保存在 `$DSH_HOME/supervisor/credentials/github-token`（默认 `~/.dsh/supervisor/credentials/github-token`），目录权限 0700、文件权限 0600；本机项目共用，优先于 `GH_TOKEN` / `GITHUB_TOKEN`、GitHub CLI 和 Git 凭据助手。验证失败保留旧 Token；操作幂等记录只存请求摘要，页面不回显密钥。推送、PR 和合并使用此运行时认证；缺少凭据、权限不足或认证检查失败时，**工作概览** 显示阻塞原因并计入“需要关注”，可点击“重新检查”（自动每分钟检查一次）。凭据不进入项目配置、台账或日志。原项目的 Git 历史、索引及未提交文件备份到状态目录的 `autopilot/git-delivery/<project>/<date>/source-backup.tar.gz`（排除可重建的依赖及构建缓存）。远端为空时，既有提交初始化目标分支，未提交源码通过基线 PR 进入评审；基线合入前暂停新任务派发。原工作区、历史独立快照和已回滚任务保留。

交付、评审和修复记录见 **代码交付与安装**。代码交付评审、修复和复验的模型用量计入 `code_delivery_tokens` 独立账本，不占项目开发每日 Token 上限，开发额度用尽也不会暂停这些交付轮次；项目设置显示独立用量。权限不足、模型不可用、漏审或检查失败会显示原因；处理阻塞原因后可“继续处理”。修复额度用尽只显示排队原因和恢复时间，次日自动恢复；旧版因累计修复上限形成的阻塞会自动按每日额度重新调度。GitHub 分支保护及必需评审仍生效。异步调用与回执、PR 编号、被评审提交和合并 SHA 均持久化，重启后核对原操作结果，不盲目重复创建 PR 或合并。

代码评审节点按需求展示其 `feat → release` PR，默认筛选未合并且未关闭，支持查看全部需求 PR。`release → master` PR 独立展示在「待合并」，不会混入需求代码评审。历史多个需求共用的真实 PR 会标注共用关系，不伪造独立 PR。状态为「未评审」「已评审 · 评审通过」「已评审 · 问题修复」「已合并」；节点数字仅统计 GitHub 仍开放且「未评审」的 PR；已关闭、已合并以及远端状态未知的 PR 不计入。已关闭但未合并的 PR 留存在列表并单独标注 GitHub 关闭状态。GitHub 状态每分钟同步并保留最近快照，失败时明确提示；评审通过必须对应当前 head/base，提交变化或修复成功后需重新评审。问题修复节点逐条展示未完成问题，同一 PR 可有多条；重复问题按 PR、文件和内容去重，修复成功后移出，复审再次发现的问题重新进入。原始轮次回执继续保存在证据台账。`GET /products/<id>/delivery-board` 返回 PR 与未完成问题视图。

接口沿用现有鉴权、操作 UUID 和版本控制：`POST /products/<id>/configure` 支持 `git`、`code_review`；`POST /products/<id>/migrate-git` 建立基线批次；`GET /deliveries`、`GET /code_reviews` 查询交付与轮次；`POST /deliveries/<id>/retry` 恢复已阻塞批次。已启用的 Git 配置变更要求没有运行中的任务和未结束交付；尚未启用时可先登记仓库地址。代码评审／修复模型可在运行中调整，仅下一轮使用新配置。


### 中文异常与应用自动恢复

控制台默认展示中文异常原因及处理建议；原始错误码、路径和日志放在默认折叠的“技术详情”，凭据会脱敏。前后端共用 `dashboard/src/error-catalog.json`，新增错误类型应同时补充分类与展示测试。历史回执在展示时兼容，不需要重写台账。

项目设置 → 运行策略提供“应用自动恢复”。默认关闭；启用后，在“自主运行”或“仅监测”模式下，正式应用退出（包括主动退出）会重新启动。暂停项目或关闭开关即可停止新的恢复尝试。系统只启动配置的正式应用，不强退已有进程或会话；发布、回滚期间延后恢复。启动等待 90 秒，失败后等待 60 秒、120 秒，连续三次失败后冷却 15 分钟。恢复记录与退避时间持久化，持续健康五分钟后清零计数。

监测端口来自与项目路径、数据目录对应的当前宿主进程，并在发送监测凭据前核对应用身份，不再信任历史日志中的端口。“立即尝试恢复”沿用管理 API 的版本校验和操作 UUID，接口为 `POST /api/products/<id>/recover-runtime`；需已开启恢复且没有冲突操作。
