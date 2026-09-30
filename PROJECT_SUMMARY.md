# DeepSeek ↔ GPT 监工工作台项目总结

更新日期：2026-09-30  
项目目录：`/Users/ranrui/Desktop/dsh-gpt-tools`

## 一、交付概况

已实现本机运行的中文管理看板，包含 React 前端、Python 管理服务、共享审查核心、Harness 连接插件，以及安装和日常使用两个管理端 skill。

看板从真实会话、审查记录和服务接口读取数据，支持人工审批与会话操作。管理服务独立运行，停止监工后仍可访问看板并重新启动监工。

代码与自动化测试已交付；当前机器的旧版审查服务尚未切换，真实 Harness 插件仍未连接，因此真实会话的完整操作链路仍待联调。

## 二、技术结构

| 模块 | 实现与职责 |
| --- | --- |
| 前端 | React + Vite + TypeScript，使用 React Router、Ant Design、TanStack Query |
| 管理服务 | Python，默认 `127.0.0.1:13084`，提供 `/api` 接口并托管前端构建产物 |
| 审查桥 | 共享审查核心与阻塞式 `/handoff` 协议，新版默认端口 `13083` |
| Harness 插件 | TypeScript Host 插件，使用 `apiProxy.sessions.prompt` 的 `steer` 模式与 `cancel` |
| 存储 | SQLite 保存配置、审批模式、操作记录和指令状态；保留审查 JSON、日志和历史记录 |
| 本地访问控制 | 回环地址监听，网页使用 Cookie、CSRF 和来源校验，插件使用独立令牌 |

前端定期刷新，页面隐藏时降低刷新频率，提供加载、空数据、连接异常和重试反馈。插件主动连接管理服务，不依赖 Harness 浏览器页面持续打开。

## 三、已实现页面

| 页面 | 内容 | 操作 |
| --- | --- | --- |
| 总览 | 服务状态、会话数量、审查进度、人工待办、每日额度、近期异常 | 启动与停止监工，进入待办 |
| 会话管理 | 标题、工作区、最近活动、交接与审批状态 | 搜索、查看详情、即时插话、停止当前轮次、切换人工审批 |
| 审查中心 | 审查阶段、状态、GPT 建议、问题与暂停证据、日志 | 观察审查、重试、取消、接管、批准、要求修改、验收通过 |
| 操作日志 | 服务事件、审查事件、人工操作、消息送达记录 | 检索筛选，跳转关联会话或审查 |
| 设置 | 模型、超时、额度、会话与历史目录、插件状态 | 保存配置、检查连接与目录匹配 |

子代理会话以只读方式展示。缺失数据保持为空或不可用，不生成虚构统计。

## 四、关键业务规则

### 审查与人工接管

- 默认由 GPT 自动审查；可提前为会话启用人工审批，也可在审查完成前接管。
- 人工决策携带记录版本；人工提交与模型完成通过原子状态更新协调，同一审查只生成一次最终结果。
- 过期操作返回冲突，需刷新记录后重新判断。
- 保留暂停证据检查；证据失败、取消、超时或服务退出均返回 `blocked`。
- 模型审查默认 240 秒，排队、模型审查与人工等待合计最多 540 秒，为客户端 600 秒超时留出余量。
- 相同 request ID 返回缓存；新复审使用新请求，保留历史结果并正常计入额度。
- 页面手动审查属于观察模式，显示暂停未验证，不会自动批准或恢复开发任务。

### 会话指令

- 即时插话使用 `steer`；停止当前轮次是独立操作。
- 交接期间插话，先终止旧审查并确认 `blocked` 返回，再向会话发送指令，避免旧结果继续放行。
- 区分“提交中／正在送达、Harness 已接收、会话已消费、失败、送达待核实”。接口接受不能证明模型完成执行。
- 使用源 `rpcId` 匹配真实会话事件，核对消息消费情况。
- 插件离线时拒绝发送；结果不确定时先核对记录，不自动重复发送。
- 停止操作沿用 Harness 语义：停止当前轮次，已排队消息可能在之后继续执行。

### 配置与历史

- 默认每日额度 12 次，按 `Asia/Shanghai` 日期记账；页面时间同样使用该时区。
- 会话目录默认取 `DSH_HOME`，未设置时取 `~/.dsh`。
- 状态目录默认取 `DSH_SUPERVISOR_STATE`，未设置时取 `<home>/supervisor`。
- 模型、超时与额度变更用于后续审查。
- 旧历史目录需在设置中显式选择；读取历史不会自动迁移或改写原数据。

## 五、安装与启动

环境要求：Node.js 22.12+、npm、Python 3.10+。真实模型审查还需要 Git、zstd 和已登录的 Codex CLI。Harness 插件安装需使用目标实例对应的 dsh CLI、profile 和 pnpm。

在项目根目录执行：

```sh
npm ci
npm run build
npm start
```

看板地址：<http://127.0.0.1:13084>。首次启动仅运行管理服务，再从总览页启动监工。若已有匹配的管理服务在运行，直接访问即可。

开发模式在管理服务之外运行：

```sh
npm run dev
```

开发页面默认使用 `http://127.0.0.1:5173`，由 Vite 代理管理 API。

### 连接 Harness

在核实目标 Harness 实例和 profile 后安装插件：

```sh
dsh plugin --profile 你的profile add /绝对路径/dsh-gpt-tools/connector
```

加载对应 profile，在设置页确认“插件在线”和“目录匹配”均成立。若使用自定义目录或端口，配置插件的 `stateDir`、`home`、`origin`；令牌由插件从管理状态目录自行读取。

已有旧版审查服务时，应确认原实例空闲后，通过原 CLI 和原状态目录停止，再启动新版；保留额度与历史数据。完整操作见项目 `README.md`。

## 六、交付的 Skills

### 1. 安装 skill：`dsh-supervisor-install`

负责环境检查、构建看板、启动管理服务、旧版切换、插件配置与连接验收。

- 项目源码：`skills/dsh-supervisor-install/`
- 本机安装：`/Users/ranrui/.codex/skills/dsh-supervisor-install/`
- 单独压缩包：`dist/skills/dsh-supervisor-install.zip`

调用示例：

```text
使用 $dsh-supervisor-install 安装 /Users/ranrui/Desktop/dsh-gpt-tools，连接我指定的 Harness 实例。
```

### 2. 使用 skill：`dsh-supervisor-use`

负责读取状态、查找会话、处理人工审批，以及按明确指令插话或停止指定会话。附带 Python 标准库 API 客户端，处理 Cookie、CSRF 和操作 ID，写操作不会自动重试。

- 项目源码：`skills/dsh-supervisor-use/`
- 本机安装：`/Users/ranrui/.codex/skills/dsh-supervisor-use/`
- 单独压缩包：`dist/skills/dsh-supervisor-use.zip`

调用示例：

```text
使用 $dsh-supervisor-use 查看监工状态，列出等待人工审批的审查。
```

两个 skill 的合并包为 `dist/skills/dsh-supervisor-skills.zip`。安装到其他机器时复制完整目录，保留 `SKILL.md`、references、agents 和随附脚本；安装 skill 仍需指向项目源码。

原有 `dsh-gpt-supervisor` skill 用于 DeepSeek 任务内部的 `plan → checkpoint → acceptance` 阻塞交接。它与这两个管理端 skill 分工不同，继续保留。

## 七、验证结果

前序实现阶段已通过以下检查，本次文档整理未重新运行整套测试：

| 检查 | 结果 |
| --- | --- |
| Python 后端测试 | 21 项通过 |
| Harness 插件测试 | 6 项通过 |
| Playwright 浏览器集成测试 | 5 项通过 |
| Skill API 脚本测试 | 4 项通过 |
| TypeScript 类型检查与生产构建 | 通过 |
| 两个 skill 的结构校验 | 通过 |
| Git diff 空白检查 | 通过 |

合计 36 项自动化测试，覆盖审查状态与竞争、重复请求、取消与超时、额度、暂停证据、恢复、指令回执，以及页面操作。另完成桌面和窄屏截图检查。

浏览器集成测试启动真实管理 HTTP 服务、桥接子进程和连接插件，但模型执行与 Harness API 使用确定性测试替身。该结果不能替代真实 Harness 与真实模型的最终联调。

## 八、当前运行状态与待办

以下为 2026-09-30 整理文档时，通过管理 API 读取的状态快照；后续可能变化。

| 项目 | 实际状态 |
| --- | --- |
| 管理 API | 可访问，地址 `http://127.0.0.1:13084` |
| 会话 | 读取到 88 个真实会话 |
| 审查服务 | 端口 `13083` 上有服务运行，但 `compatible=false`，仍为旧版 |
| Harness 连接插件 | `online=false`，未建立连接 |
| 目录匹配 | `home_matches=false`，当前没有插件报告可供核对的 home |
| 当日额度 | 已用 4 次，上限 12 次 |
| 新审查状态统计 | 当前为空 |

尚待完成：

1. 核实旧版审查实例与活动任务，在空闲时切换至兼容新版。
2. 在实际使用的 Harness profile 安装并加载连接插件，确认在线及目录一致。
3. 使用独立测试会话与真实模型验证自动审查、人工批准、要求修改、即时插话和停止流程。
4. 核对页面状态与实际会话事件，完成真实环境验收。

## 九、维护入口

```text
dashboard/                         前端页面、组件与样式
connector/                         Harness Host 连接插件
dashboard_server.py                管理 API、静态资源与进程管理
dsh-gpt-supervisor/scripts/
  review_core.py                   共享审查核心与持久化逻辑
  bridge.py                        通用 CLI 与阻塞交接协议
dsh_bridge.py                      兼容入口，默认端口 13081
skills/                            安装与使用 skill 源码
.agent/skills/                     仓库内分发副本
scripts/sync-skills.py              分发副本同步脚本
tests/                             后端、skill 与浏览器测试
README.md                          完整启动、插件与操作说明
```

常用验证命令：

```sh
npm run typecheck
npm test
npm run test:e2e
npm run sync-skills
```

修改 skill 或共享桥接实现后同步仓库分发副本，再按需更新本机已安装副本与压缩包。构建产物、运行状态和插件令牌不提交到 Git。
