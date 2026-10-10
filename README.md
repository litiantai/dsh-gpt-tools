# DeepSeek ↔ GPT 监工工作台

## 通用仓库接入与自进化

点击首页的「接入仓库」，提供本地 Git 目录或 HTTPS Git URL。平台在独立工作区记录技术栈、命令来源、依赖、源码提交、构建与测试结果、启动健康、截图及停止结果。启动验证与业务验收分开记录；缺少启动入口、隔离依赖或凭据时显示阻塞，不生成成功证据。重试建立关联的新扫描，原记录不变。通过后可登记为观察模式，或通过版本校验将配置应用到同一项目。

项目可通过 `.autopilot.json` 声明版本 1 配置：`commands` 中的 `install/build/test/browser/start` 均为命令参数数组列表，启动命令支持 `{port}` 与 `{state}`。`readonly_paths` 限定正式实例只读断言，`acceptance_checks` 提供命名的隔离测试（探针路径为 `check:名称`）。`artifacts` 声明产物目录，`exclude` 排除不属于发布基线的材料。仓库内本项目配置是完整示例。

验收分为发布前候选验证与发布后正式实例复验。开发、独立业务验证和方案/验收审查统一使用 `pre_release` 阶段：核对当前 feat/release 提交、功能和真实测试证据；正式实例尚未更新不作为候选验收失败原因。原需求及 `resolution_probes` 保留，候选回执与产物清单中的 `acceptance_scope.post_release` 标记为 `pending`。正常发布并核对运行版本后，控制器通过上线观察或主分支最终复验执行原始断言；候选测试通过不能代替正式实例效果验证。

仓库扫描页面先核对 `/api/runtime-identity` 的 `capabilities.repository_scans`。运行后端声明整数版本 1 或以上才允许查询、提交、重试和应用扫描；旧服务或身份查询失败时停止扫描请求，并提供升级提示与重新核对入口。

依赖安装中明确的网络错误（如 `ECONNRESET`）保留在验证阶段等待重试，不消耗代码返修次数；依赖定义冲突、构建或测试失败仍按原返修规则处理。

项目设置的「通知」支持邮件账户、收件人和五类事件配置。授权码只保存在本机凭据文件，API 不回显。投递记录逐个保存 SMTP 接受和拒收结果，部分拒收仅重试尚未接受的收件人，耗尽次数显示「部分发送失败」。纯文本与 HTML 邮件均包含事件重点。SMTP 接受不能证明收件箱已收到邮件；实际送达需在配置后由用户发送测试简报并核对收件箱。本地协议测试只连接临时回环 SMTP 服务，不使用真实账户或向外发送邮件。

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

## 通用仓库接入与自进化

点击首页的「接入仓库」，提供本地 Git 目录或 HTTPS Git URL。平台在独立工作区记录技术栈、命令来源、依赖、源码提交、构建与测试结果、启动健康、截图及停止结果。启动验证与业务验收分开记录；缺少启动入口、隔离依赖或凭据时显示阻塞，不生成成功证据。重试建立关联的新扫描，原记录不变。通过后可登记为观察模式，或通过版本校验将配置应用到同一项目。

项目可通过 `.autopilot.json` 声明版本 1 配置：`commands` 中的 `install/build/test/browser/start` 均为命令参数数组列表，启动命令支持 `{port}` 与 `{state}`。`readonly_paths` 限定正式实例只读断言，`acceptance_checks` 提供命名的隔离测试（探针路径为 `check:名称`）。`artifacts` 声明产物目录，`exclude` 排除不属于发布基线的材料。仓库内本项目配置是完整示例。

验收分为发布前候选验证与发布后正式实例复验。开发、独立业务验证和方案/验收审查统一使用 `pre_release` 阶段：核对当前 feat/release 提交、功能和真实测试证据；正式实例尚未更新不作为候选验收失败原因。原需求及 `resolution_probes` 保留，候选回执与产物清单中的 `acceptance_scope.post_release` 标记为 `pending`。正常发布并核对运行版本后，控制器通过上线观察或主分支最终复验执行原始断言；候选测试通过不能代替正式实例效果验证。

仓库扫描页面先核对 `/api/runtime-identity` 的 `capabilities.repository_scans`。运行后端声明整数版本 1 或以上才允许查询、提交、重试和应用扫描；旧服务或身份查询失败时停止扫描请求，并提供升级提示与重新核对入口。

依赖安装中明确的网络错误（如 `ECONNRESET`）保留在验证阶段等待重试，不消耗代码返修次数；依赖定义冲突、构建或测试失败仍按原返修规则处理。

项目设置的「通知」支持邮件账户、收件人和五类事件配置。授权码只保存在本机凭据文件，API 不回显。投递记录逐个保存 SMTP 接受和拒收结果，部分拒收仅重试尚未接受的收件人，耗尽次数显示「部分发送失败」。纯文本与 HTML 邮件均包含事件重点。SMTP 接受不能证明收件箱已收到邮件；实际送达需在配置后由用户发送测试简报并核对收件箱。本地协议测试只连接临时回环 SMTP 服务，不使用真实账户或向外发送邮件。

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


## 角色工作流与语言质量门禁

需求发现、开发、独立验证、代码评审、验收五类角色在 `skills/dsh-role-*/workflow.yaml` 声明内部有序步骤。`SKILL.md` 保留角色职责；共享语言标准位于 `skills/dsh-role-verification/profiles/`。总览图、主阶段及返修机制不变，记录详情展示实际工作流和检查结果。“已提交，待核对”是模型声明，只有控制器命令回执可以表示质量检查通过。

工作流版本为 1，步骤包含 `id`、`title`、`kind`（instruction / runtime / quality），可用 `actions` 与 `stacks` 限定适用范围，用 `rules` 引用 skills 根目录内的规则文件。`profiles` 将技术栈映射到共享标准；标准中的 `phases` 指定命令名称与 `required`（always / configured）。顺序固定为 install → compile → typecheck → build → test → browser。Java 必需 compile/test；React、Vue 必需 build/test；其他栈沿用项目已配置命令。平台保存规则、跨角色引用、输出契约和内容摘要；已建立的调用继续使用原快照，配置错误明确阻塞。

`.autopilot.json` 新增可选的 `compile`、`typecheck` 命令及 `stack`、`modules`。命令仍为参数数组列表；模块使用独立的命令和相对目录，不继承根目录命令。存在 modules 时，根命令作为通用聚合检查；服务启动仍使用项目根目录的唯一 start/test_start（多服务项目应声明自己的组合启动入口）。支持 generic、node、python、java、react、vue；缺少 stack 时从模块源码识别。

新扫描登记 `workflow_version: 1`，成果验收必须具有完整控制器回执。旧调用和未配置工作流的角色保持兼容；已有通用项目下一次运行验证会生成质量回执。回执绑定提交、源码摘要、配置、工作流、命令、退出码与日志摘要。重复核对相同回执无需重新执行；源码、配置、日志或回执变化后不可复用。代码评审先运行安装、编译、类型检查、构建和测试，完整运行验证随后执行服务和浏览器验收。

单模块 Maven 示例（项目自行提供可用 JDK、Maven 和明确的依赖来源）：

```json
{
  "version": 1,
  "workflow_version": 1,
  "stack": "java",
  "install_policy": "jvm-resolve",
  "commands": {
    "install": [["mvn", "dependency:go-offline"]],
    "compile": [["mvn", "compile"]],
    "test": [["mvn", "test"]],
    "start": [["java", "-jar", "target/app.jar", "--server.port={port}"]],
    "build": [["mvn", "package", "-DskipTests"]]
  }
}
```

JVM 安装策略 `jvm-resolve` 显式允许依赖解析阶段执行 Maven 插件或 Gradle 构建脚本；隔离执行阶段强制离线，缓存保存在本次控制器执行目录。Maven 安装只接受 `dependency:go-offline`；Gradle 安装只接受项目自定义的 `autopilotResolveDependencies`，该任务应解析所有后续检查所需的可解析配置，例如：

```groovy
tasks.register('autopilotResolveDependencies') {
    doLast {
        allprojects.each { p ->
            p.configurations.findAll { it.canBeResolved }.each { it.resolve() }
        }
    }
}
```

Gradle 对应命令为 `["gradle", "autopilotResolveDependencies"]`、`["gradle", "classes"]`、`["gradle", "test"]`。只使用本机已安装的 Maven/Gradle；wrapper 可能下载工具链，因此提示配置本机工具路径，不自动执行下载。控制器设置独立 Maven/Gradle 缓存与 JAVA_HOME，并禁用 Gradle 自动下载 JDK；缺少 JDK 或构建工具时提示安装所需版本、设置 JAVA_HOME/PATH 或配置绝对路径，不自动下载运行时；缺少离线依赖或启动入口记录阻塞，编译与测试断言错误记录失败。隔离后端仍仅支持 macOS。

混合模块可在相同配置中声明 `modules: [{"id":"backend","path":"backend","stack":"java","commands":{...},"install_policy":"jvm-resolve"},{"id":"frontend","path":"frontend","stack":"react","commands":{...}}]`。所有模块先完成编译再进入测试，任一必需步骤失败即停止。Watch 仅检查运行身份、健康、只读业务信号并生成需求线索，不执行源码编译。Spring Boot JavaDoc 标准在共享 Java 规则中定义，只审查本次新增或修改的相关代码。

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

项目可配置 `functional_findings_policy: "backlog"`：release 的必需本机测试通过后，独立业务验收发现的功能缺陷保留原 `fail` 证据，并去重登记为 P0 高优先级需求，不再阻拦合并。回执显示需求 ID 与非阻塞处置；实际测试失败、验证执行异常或未完成验证仍阻断，开发阶段验收规则不变。

本机分支测试可在已登记项目的管理配置中设置 `test_execution: "local"`。开发控制器为任务建立 `feat-<任务 ID>` 工作区；待合并验收使用对应 `release-xxx` 工作区。控制器核对分支、提交与干净工作区后，使用本机工具链安装、构建，启动一个临时实例并执行单元及浏览器测试。测试不会在模型沙箱内运行；真实 HOME 保持不变，端口、临时文件、状态和日志独立，超时或验收结束后回收进程组。日志和实例记录绑定分支及提交，失败不得当作通过。

`.autopilot.json` 的 `test_start` 可指定测试实例入口；本项目使用实际看板的测试夹具入口。Playwright 收到控制器提供的 `DSH_E2E_EXTERNAL_ORIGIN` 后复用该实例，不再重复启动。独立执行 `npm run test:e2e` 时仍自行启动测试夹具。模型开发阶段只改代码，验收模型读取控制器提供的真实测试证据。需求、调查及复盘读取当前 master 的独立只读工作区，巡检与合并后复验仍指向 master 实例。其他项目保持原执行方式，仓库配置文件不能自行打开本机执行模式。

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

`GET /api/reviewers/codex/account` 通过配置的 Codex CLI 实时查询账户与额度，不发送生成请求，不消耗审查次数。返回账户标识、查询时间、全部额度窗口和服务端使用许可；空值表示未知，查询失败不复用旧快照。代码交付评审及修复在启动前使用本轮实际 CLI 和认证目录重新查询，将结果保存为 `account.json` 并附在回执中。仅服务端明确拒绝使用或未登录时提前阻塞；查询暂不可用时继续由真实执行结果判断。当前可用不保证整轮执行期间额度始终充足，历史失败记录不会被当前额度覆盖。

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

业务验收通过后，每个需求的增量进入其固定 `feat-<需求标识>` 分支，并绑定唯一一个 `feat → release` 代码评审 PR；返修与重试复用原 PR，跨日延期将原 PR 的目标切换到次日 release 并重新评审，不为同一需求重复建 PR。每次只准备、评审并合入一个需求，其他需求仍排队。`release-YYYYMMDD` 默认每天一个批次、从 `master` 创建，代码评审通过前禁止向其合入成果；评审失败记录问题，独立修复后重新评审。该阶段只使用固定 OCR 规则与用户所选模型逐文件 Code Review，不启动客户端。每个评审 MR 地址、提交和实际模型分别保存；同日可有多个已合并的评审 MR，同一批次共用一个 release 和一个 `release → master` 上线 PR。

成果真正合入 release 后才标记“已交付未上线”。北京时间 **23:30 封板**，不再开启新的代码评审；未合入成果及修复补丁保留到次日重新评审。封板后的 release 只针对实际已合入成果执行必需检查和业务回归，不带入延期任务的验收条件，也不逐个成果启动客户端验证。统一验证通过后优先按日期顺序合入 master；仅因修复额度排队的批次不占住后续合格成果的上线队列。目标分支或 release 变化会使验证证据失效，冲突修复后重新同步并验证；release → master 不再执行代码复审，也不等待次日评审窗口。只有 GitHub 确认上线 PR 已合并并返回 merge SHA，关联需求才显示“已上线”；客户端安装和回滚独立记录。

「待合并 · Release PR」支持 **手动合并**：批次完成需求汇集后，可提前封板并异步执行同步、统一验证和合并，无需等待 23:30；仍遵守 GitHub 检查与分支保护。请求提交后立即返回，合并确认仍更新需求上线状态并继续主实例更新与最终复验。同日后续成果使用 `release-YYYYMMDD-2`、`-3` 等独立批次及工作区，不复用已封板的 PR。执行中、结果待核对或尚未完成汇集的批次不可重复提交。 已异常阻塞的 release 行显示「重试」，从上次中断阶段继续同步、验证或合并；旧版 release 复审异常重试时改走同步和验证；执行中或回执待核对时禁用，提交后立即刷新进度。

首次接入先保存配置，再点击 **建立首次基线 PR**。服务检查本机 GitHub 认证的推送权限和 merge commit 支持；可在 **Git 与代码评审 → 本机 GitHub Token** 直接输入并验证保存。Token 保存在 `$DSH_HOME/supervisor/credentials/github-token`（默认 `~/.dsh/supervisor/credentials/github-token`），目录权限 0700、文件权限 0600；本机项目共用，优先于 `GH_TOKEN` / `GITHUB_TOKEN`、GitHub CLI 和 Git 凭据助手。验证失败保留旧 Token；操作幂等记录只存请求摘要，页面不回显密钥。推送、PR 和合并使用此运行时认证；缺少凭据、权限不足或认证检查失败时，**工作概览** 显示阻塞原因并计入“需要关注”，可点击“重新检查”（自动每分钟检查一次）。凭据不进入项目配置、台账或日志。原项目的 Git 历史、索引及未提交文件备份到状态目录的 `autopilot/git-delivery/<project>/<date>/source-backup.tar.gz`（排除可重建的依赖及构建缓存）。远端为空时，既有提交初始化目标分支，未提交源码通过基线 PR 进入评审；基线合入前暂停新任务派发。原工作区、历史独立快照和已回滚任务保留。

交付、评审和修复记录见 **代码交付与安装**。代码交付评审、修复和复验的模型用量计入 `code_delivery_tokens` 独立账本，不占项目开发每日 Token 上限，开发额度用尽也不会暂停这些交付轮次；项目设置显示独立用量。权限不足、模型不可用、漏审或检查失败会显示原因；处理阻塞原因后可“继续处理”。修复额度用尽只显示排队原因和恢复时间，次日自动恢复；旧版因累计修复上限形成的阻塞会自动按每日额度重新调度。GitHub 分支保护及必需评审仍生效。异步调用与回执、PR 编号、被评审提交和合并 SHA 均持久化，重启后核对原操作结果，不盲目重复创建 PR 或合并。

独立验证以控制器在未沙箱化侧落盘的 `base..commit` 自包含差异（`verification-diff.patch` / `verification-facts.json`）与源码摘要为权威事实证据，并把 `diff_file`、`facts_file`、`diff_sha256`、`changed_files`、`merge_base` 写入「独立业务验证」检查回执以便离线核对。Git 元数据不可读（EPERM/permission denied）不构成 blocked 理由；控制器证据完整且必需检查通过时，若验证者仍以 Git 元数据为由 blocked，控制器会明确「git 不可读不得作为理由」并用同一份证据纠正性重试一次，两次仍 blocked 才保留 blocked 并记录重试说明，绝不把 blocked 改写成 pass。

代码评审节点按需求展示其 `feat → release` PR，默认筛选未合并且未关闭，支持查看全部需求 PR。`release → master` PR 独立展示在「待合并」，不会混入需求代码评审。历史多个需求共用的真实 PR 会标注共用关系，不伪造独立 PR。状态为「未评审」「已评审 · 评审通过」「已评审 · 问题修复」「已合并」；节点数字仅统计 GitHub 仍开放且「未评审」的 PR；已关闭、已合并以及远端状态未知的 PR 不计入。已关闭但未合并的 PR 留存在列表并单独标注 GitHub 关闭状态。GitHub 状态每分钟同步并保留最近快照，失败时明确提示；评审通过必须对应当前 head/base，提交变化或修复成功后需重新评审。问题修复节点逐条展示未完成问题，同一 PR 可有多条；重复问题按 PR、文件和内容去重，修复成功后移出，复审再次发现的问题重新进入。原始轮次回执继续保存在证据台账。`GET /products/<id>/delivery-board` 返回 PR 与未完成问题视图。

接口沿用现有鉴权、操作 UUID 和版本控制：`POST /products/<id>/configure` 支持 `git`、`code_review`；`POST /products/<id>/migrate-git` 建立基线批次；`GET /deliveries`、`GET /code_reviews` 查询交付与轮次；`POST /deliveries/<id>/retry` 恢复已阻塞批次；`POST /deliveries/<id>/manual-merge` 提交手动合并（需当前 `version`）。已启用的 Git 配置变更要求没有运行中的任务和未结束交付；尚未启用时可先登记仓库地址。代码评审／修复模型可在运行中调整，仅下一轮使用新配置。


### 中文异常与应用自动恢复

控制台默认展示中文异常原因及处理建议；原始错误码、路径和日志放在默认折叠的“技术详情”，凭据会脱敏。前后端共用 `dashboard/src/error-catalog.json`，新增错误类型应同时补充分类与展示测试。历史回执在展示时兼容，不需要重写台账。

项目设置 → 运行策略提供“应用自动恢复”。默认关闭；启用后，在“自主运行”或“仅监测”模式下，正式应用退出（包括主动退出）会重新启动。暂停项目或关闭开关即可停止新的恢复尝试。系统只启动配置的正式应用，不强退已有进程或会话；发布、回滚期间延后恢复。启动等待 90 秒，失败后等待 60 秒、120 秒，连续三次失败后冷却 15 分钟。恢复记录与退避时间持久化，持续健康五分钟后清零计数。

监测端口来自与项目路径、数据目录对应的当前宿主进程，并在发送监测凭据前核对应用身份，不再信任历史日志中的端口。“立即尝试恢复”沿用管理 API 的版本校验和操作 UUID，接口为 `POST /api/products/<id>/recover-runtime`；需已开启恢复且没有冲突操作。


项目可配置 `functional_findings_policy: "backlog"`：release 的必需本机测试通过后，独立业务验收发现的功能缺陷保留原 `fail` 证据，并去重登记为 P0 高优先级需求，不再阻拦合并。回执显示需求 ID 与非阻塞处置；实际测试失败、验证执行异常或未完成验证仍阻断，开发阶段验收规则不变。

本机分支测试可在已登记项目的管理配置中设置 `test_execution: "local"`。开发控制器为任务建立 `feat-<任务 ID>` 工作区；待合并验收使用对应 `release-xxx` 工作区。控制器核对分支、提交与干净工作区后，使用本机工具链安装、构建，启动一个临时实例并执行单元及浏览器测试。测试不会在模型沙箱内运行；真实 HOME 保持不变，端口、临时文件、状态和日志独立，超时或验收结束后回收进程组。日志和实例记录绑定分支及提交，失败不得当作通过。

`.autopilot.json` 的 `test_start` 可指定测试实例入口；本项目使用实际看板的测试夹具入口。Playwright 收到控制器提供的 `DSH_E2E_EXTERNAL_ORIGIN` 后复用该实例，不再重复启动。独立执行 `npm run test:e2e` 时仍自行启动测试夹具。模型开发阶段只改代码，验收模型读取控制器提供的真实测试证据。需求、调查及复盘读取当前 master 的独立只读工作区，巡检与合并后复验仍指向 master 实例。其他项目保持原执行方式，仓库配置文件不能自行打开本机执行模式。


## AI 对话、竞品研究与角色协作

项目中的「AI 对话」采用纯对话界面：左侧「项目简介」抽屉提供项目介绍、切换与模型设置，右侧「历史」抽屉提供历史对话及新对话；两侧默认关闭，中央只保留消息和输入框。对话支持多轮需求澄清与项目进展问答，查询结果使用项目台账及更新时间；截图和文档可以作为证据。界面支持 360–430px 手机宽度，但服务仍只绑定本机，未开放外网访问。

### 需求确认与附件

对话及竞品分析形成的需求进入「待确认」。补齐目标、范围、用户价值、证据与验收条件，解决待澄清事项后在对话中回复「确认」。多项需求回复「确认全部」或「确认第1项」；确认绑定助手展示的版本，旧版本不能覆盖已修改的草稿。服务端保存确认快照，并在同一事务中确认与排队；缺少有效效果探针时先调查。暂停项目仍能对话和确认需求，研发等待项目恢复。直接在对话中说明修改内容，助手返回完整新草稿；已确认的范围修改建立关联变更草稿，不能直接修改执行基线。旧需求沿用原有流程。

附件支持 PNG/JPEG/WebP、PDF、DOCX、UTF-8 TXT/Markdown，每条消息最多五个、每个 10 MB。PDF 正文提取需要在**运行管理服务的 Python 环境**安装依赖：

```sh
python3 -m pip install -r requirements.txt
```

加密 PDF、超过 200 页、没有可提取正文的扫描 PDF、超过 10 万字符的文档会显示解析失败；扫描 PDF 可改用图片上传。图片通过执行器图片通道读取；当前隔离 Harness 未开放图片和搜索通道时明确阻塞，不会静默换模型。附件在状态目录的 `autopilot/attachments/<project-id>` 中按项目存储；读取校验归属和 SHA-256。

### 竞品研究

「竞品分析」与「AI 对话」为同级标签，可直接查看竞品名单与研究记录、发现新竞品或开始分析。在「项目设置 → 竞品分析配置」中选择竞品分析模型并开启周期研究。对话中发送「发现竞品」「分析竞品」「查看竞品」可手动操作；「停用竞品 名称」「启用竞品 名称」维护名单状态，研究完成后回到原对话。默认每周一北京时间 01:00 发现新竞品，每天 02:00 分析已有名单，周一优先完成竞品发现；也可手动触发。项目暂停时停止周期派发，恢复后补最近到期窗口；关闭周期研究不会清除名单或报告。对话和研究模型默认继承需求发现角色，可分别选择其他已有执行器与模型；对话模型仍在项目简介抽屉的「模型与运行设置」中配置。

发现阶段必须取得联网搜索工具回执并实际读取候选公开页面。名单支持人工添加、编辑、停用及合并；停用或合并的竞品不会因相同名称或来源被自动恢复。分析保存 URL、采集时间、内容指纹、正文摘录与错误；只有成功分析的快照成为下一次比较基线。无变化不调用差距分析模型、不生成需求；采集或模型失败可以重试。研究产生的需求仍需人工确认。

保存监管系统自身项目的智能设置时，通过仓库身份匹配预置 Devin 与 Claude Code 的官方资料，不另建重复项目。未接入本仓库时，先使用既有「接入仓库」流程。

### 异步 Agent 协作

开启「Agent 异步协作」后，方案、开发、验证及审查可在阶段边界提出问题。调度器绑定发送角色，持久化消息后按需唤醒需求、开发、测试或审查角色；被咨询角色只读工作区。等待回复不占开发执行槽位，工作区及原阶段保留；回复后恢复原阶段，正式审查重新执行，消息不能充当审批或发布授权。

任务详情的协作记录可查看入队、送达、消费、回复和证据。依赖环、同主题超过三轮、模型失败转人工处理；问题同步到「任务协作待答」对话，在对话中直接回复后续跑。传输和消费按调用 ID 去重，回执处理及回复恢复使用事务；重启不会将未回复视为完成。

新增 API 位于 `/api/products/<id>/intelligence/`：`conversations`、`chat_messages`、`attachments`、`competitors`、`research_jobs`、`source_snapshots`、`agent_messages` 和 `settings`。列表支持项目内 `cursor/limit` 分页及会话、任务、状态过滤；详情路径为 `<resource>/<id>`。消息提交包含 `conversation_id/content/attachment_ids/reply_to`；研究任务提交 `action=find_competitors|analyze_competitors`。需求操作为 `/api/requirements/<id>/edit|confirm|reject|amend`，人工回复为 `agent_messages/<id>/reply`。写入沿用操作 UUID、Cookie、CSRF 与版本检查，附件内容不存入操作日志。

对话 Token 单独计入 `chat_tokens`，竞品研究计入 `discovery_tokens`，研发协作计入开发账本。所有模型调用遵守项目供应商运行时段；平台更新排空会等待这些独立进程退出。周期研究和角色协作默认关闭，历史记录只读保留。

验证使用 `tests/test_intelligence.py`、`tests/browser/intelligence.spec.ts` 及既有全量测试；模型测试采用确定性替身，不能替代实际账户下的联网搜索和视觉模型联调。


平台更新期间，各页面顶部显示当前阶段、受影响操作和健康观察倒计时。`GET /api/platform-update` 返回更新状态；`POST /api/platform-update/release` 携带当前 `job_id`、`commit` 和操作 UUID，允许在更新锁期间请求手动结束观察。独立更新器再次核对服务版本、数据库和调度心跳后解除锁，记录实际观察时长与手动操作；切换、启动检查及回滚阶段不可强制解除。解除更新锁不改写项目原有暂停状态或测试回执。

项目页的“同步 master”按钮会打开执行终端，将远端最新主分支逐个合入 `feat-*`、`release-*`。终端持续显示命令、日志、冲突方案和各分支结果；先保存冲突方案再调用修复模型。正在执行任务或有未提交改动的分支会跳过并提示原因，失败的冲突工作区保留；可处理后重新同步。远端已有分支使用普通 push，拒绝覆盖并发提交，本地独有分支保持本地。此操作不会修改 master，也不会把同步结果当作测试或验收通过。

## Computer Use 测试链路

项目一级导航的「测试链路」提供链路库、运行记录和测试配置。启用后，新确认或新排队的需求自动生成一次链路；历史需求由用户选择生成。可将已有链路关联到其他需求，编辑步骤、追加预期，或选择分支任务手动运行。每次运行固定链路版本、目标提交、模型与限制；编辑不会改变在途运行。纯界面需求完成可执行链路覆盖后可以进入研发，不要求额外编造 API 探针。

第一版执行器为 **Codex 图片输入与结构化动作 + Playwright Chromium**，统一 `ComputerUseDriver` 接口保留原生驱动扩展点；尚未接通桌面插件的后台调用。安装项目依赖后执行 `npx playwright install chromium`，并在本机完成 Codex 登录。在「测试配置」选择 Codex 模型，留空时继承项目的 Codex 验证模型。测试项目需配置分支启动命令及健康检查；依赖服务逐项登记 HTTP origin，需要登录时提供专用测试账号的 Playwright storageState 文件绝对路径。缺少浏览器、模型、测试实例或会话条件会显示阻塞原因。

控制器持有与实际提交对应的 feat/release 测试实例，使用独立浏览器上下文；每次根据新截图判断操作位置，记录动作前后截图、实际观察、判断、用量与时间。任意步骤可启用 Loop：触发后只观测，可明确允许刷新。默认间隔 30 秒、最长 30 分钟、最多 60 轮、100 个动作，均可调整。停止运行会取消模型调用并回收会话；进程或浏览器会话丢失标记阻塞，不自动重放业务操作。

链路通过即可完成本次必要 UI 验收；人工点击「确认正确并保存基准」后，才保存不可变正确结果。旧检查点保留，改变原预期需要记录替代检查点及关联变更需求。截图沿用证据台账和 SHA-256 校验；失败、缺截图或截图损坏的记录不能确认。关联链路在 feat 与 release 验证期间执行，失败进入返修，环境故障阻塞；不能通过“记录问题后继续交付”跳过，旧提交的通过也不能代替新提交。

接口位于 `/api/products/<id>/test-chains`：列表、`configure`、`generate`、`<chain-id>/retry|revise|link|run`、`runs/<run-id>`、`runs/<run-id>/cancel|confirm-baseline`，沿用项目归属、操作 UUID 和版本冲突检查。生成及截图决策加载 `dsh-role-verification` 的规则、JSON Schema 与调用快照，并共用项目辅助调度槽。

回归测试为 `tests/test_test_chains.py`、`tests/test_computer_browser.py`、`tests/test_local_testing.py` 和 `tests/browser/test-chains.spec.ts`。真实模型完整实测可运行以下命令，输出目录必须尚不存在，调用消耗所选 Codex 模型额度；样例仓库、基准、失败及返修截图全部保存于该目录：

```sh
python3 scripts/test-computer-use-smoke.py --model <可用的 Codex 模型> --output .dashboard/computer-use-smoke
```


## 项目协调与跨执行器角色 Skills

自主运行的项目默认各有一个协调 Agent，入口为「任务流水线 → 项目协调」；「项目设置 → 项目协调」可关闭或选择独立模型，未指定时沿用项目验收模型。暂停项目不会自动恢复；已运行的调用先核对回执。点击「立即评估」安排一次新检查。

协调器每 60 秒检查状态，任务和证据未变化时不调用模型。它可以调整本项目未开始任务的顺序、向角色请求只读协助、安排补证和逐轮追加返修。每次追加一轮，原返修计数保留；连续两轮没有真实检查或正式审查证明改善时，进入「AI 对话 → 协作记录」等待人工回复。修改行数和开发自述不算进展，新增回归不会判为改善。人工插队、暂停、副作用核对和正式验收仍由控制器约束。

研发任务阻塞后，人工点击「重试」单独授权一轮，不计入基础或额外返修次数，也不受默认 3 次返修上限限制；再次阻塞后可继续人工重试。返修额度耗尽时直接回到方案修订或开发修复，不重复消费旧审查结论。原返修次数、用时和执行回执保留，人工重试次数独立展示；每次点击在关联证据和平台事件中记录时间、原阻塞原因、恢复阶段及原审查引用。自动返修仍受原规则约束，人工重试不绕过 Token、执行时间、审查额度及未确认执行结果的保护。

协调调用使用 `coordination_tokens` 单独统计，不扣开发额度。协助、额外开发和验收按各自原规则计额，累计执行时限、审查额度、DeepSeek 错峰及供应商账户限制继续有效。主研发仍共用一个执行槽，聊天、研究、协助、协调及独立测试链任务共享每项目辅助槽，派发时加锁复查。

角色规则位于 `skills/dsh-role-*`，包括 `SKILL.md`、场景 `references/`、输出 `schemas/` 及必要 `scripts/`。平台通过 `autopilot/role_skills.py` 显式加载，Codex、Claude 和 DeepSeek Harness 使用同一阶段规则；供应商接口仅处理工具、沙箱和结果传输差异。Computer Use 的图片能力约束继续由其适配器检查。

每个执行目录下的 `role-skill/` 保存实际加载规则、支持文件、输出契约、内容哈希及模型信息。运行快照只读，后续技能更新不覆盖历史调用。`npm run sync-skills` 将技能同步到仓库分发目录，不安装或改写用户全局 Skills。技能中的脚本只读取证据、比较进展和校验结果；任务变更通过平台事务执行。

项目 API：`GET /api/products/{id}/coordinator` 查询状态与最近决定；`POST .../coordinator/settings` 提交 `{version, config:{enabled, model}}`；`POST .../coordinator/evaluate` 提交 `{version}`。写操作沿用 Cookie、CSRF 与 operation_id；过期版本或暂停项目会拒绝执行。

回归入口：`python3 -B -m unittest discover -s tests -p 'test_coordinator.py'`、`test_role_skills.py`，以及 `npx playwright test tests/browser/coordinator.spec.ts`。测试使用隔离状态和可控模型结果，不向真实任务派发修复。

### 项目技术栈与本机安装检测

仓库获取完成后，接入扫描先读取 Maven/Gradle、package.json 等清单，识别 Java、React、Vue、Node.js、Python 与混合模块，并保存识别依据。`.autopilot.json` 的 `stack` / `modules` 继续决定工作流选择；识别模块不会擅自生成 Java 构建命令。

在“仓库接入与启动扫描 → 扫描详情”或“项目设置 → 接入环境”点击“检测本机运行时”，可查看 JDK（java/javac）、Maven/Gradle、Node.js 及包管理器的版本、实际路径和安装提示。检测针对管理服务所在电脑，执行固定版本命令，不运行项目构建、不自动下载工具链。安装后点击“重新检测本机运行时”；修改 PATH / JAVA_HOME 后需确保管理服务也能读取新环境（必要时重启服务）。工具可运行只代表安装检测通过，项目仍需重新扫描并通过原有编译、测试及验收。

只读识别接口为 `GET /api/products/:id/environment`、`GET /api/scans/:id/environment`；手动检测为对应的 `POST .../runtime-check`，复用现有认证及 CSRF。检测不会改写项目配置或历史扫描的验收结论。
