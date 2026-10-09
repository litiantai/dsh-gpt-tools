---
name: dsh-supervisor-use
description: 使用已安装的 DeepSeek ↔ GPT 监工工作台，查看真实会话和审查、启停监工、切换人工审批、提交审查结论，以及按用户要求向指定会话即时插话或停止当前轮次。用于日常管理与故障定位，不用于首次安装，不在查看状态时修改监管范围。
---

# 使用监工工作台

通过本机看板或其管理 API 完成用户要求的操作。优先使用用户明确指定的界面；没有界面要求时使用随 skill 打包的 `scripts/control.py`，避免临时拼装 Cookie 和 CSRF 逻辑。

## 先读取当前状态

`<skill-dir>` 是当前加载 skill 的绝对目录，不是项目路径。

```sh
python3 -B "<skill-dir>/scripts/control.py" read /overview
python3 -B "<skill-dir>/scripts/control.py" read /sessions
```

默认管理地址 `http://127.0.0.1:13084`；自定义地址用全局选项 `--origin`。管理服务不可用时，先定位项目与原状态目录再启动，不要创建一套新目录来掩盖故障。需要安装或切换旧服务时使用项目 README；不要在日常操作中顺带升级整套环境。

- `service.running=true` 且 `service.compatible=true` 才代表新桥可管理。
- 向会话发指令需要 `connector.online` 与 `connector.home_matches` 都为 true。
- 通过标题、工作区和 ID 确认目标；多个候选无法区分时询问用户，不能靠最近活动猜测。子代理会话只读。
- “查看”“检查状态”只做读取。只有用户要求对应操作时才启停、修改配置或提交审批；只有明确要求向目标会话发送内容时才发送消息，检查状态不授予发消息的权限。

## 原生插件强制监管

`connector.native_supervision=true` 表示新版插件已启用自动监管；连接在线不等于当前任务已通过审查。会话 `supervision` 字段报告监管阶段和审批点。读取 `/native-gates/<id>` 查看阻塞原因、当前审查和人工放行记录。

用户明确要求重试时，向 `/native-gates/<id>/retry` 传当前 `review_id`；要求单次放行时，向 `/native-gates/<id>/release` 传 `review_id` 和明确 `reason`。保持操作 UUID 幂等。人工放行不代表 GPT 通过，也不能跳过未结束的后台任务。

## 执行与核对

读取 [操作接口与语义](references/operations.md) 中与本次任务相关的部分。写操作使用 JSON 文件传参，并为一次操作生成一个 UUID；同一次操作网络重试沿用 UUID 和完全相同的参数。

```sh
python3 -B "<skill-dir>/scripts/control.py" act /service/start --operation-id "<UUID>"
python3 -B "<skill-dir>/scripts/control.py" act "/sessions/<session-id>/messages" \
  --operation-id "<UUID>" --body-file "/绝对路径/instruction.json"
```

脚本不自动重试写请求。超时或“结果待核实”后，先读取目标会话指令记录或审查记录。不要更换 UUID 盲目重复发送；明确失败且原因处理完毕后才能作为新操作重试。

人工接管与审批前重新读取审查详情，提交当前 `version`。发生 409 时刷新、核对状态；已结束的审查不可改写。页面发起或重试的审查属于观察模式，不会暂停、批准或恢复 DeepSeek。

发送成功不等于模型已执行：报告实际状态“提交中 / 正在送达 / Harness 已接收 / 会话已消费 / 失败 / 送达待核实”。停止会话指停止当前轮次，Harness 会保留已有排队消息，之后仍可能继续运行。

完成后简要报告操作对象、结果与证据；若是查状态，列出待人工处理项即可。仅在用户要求持续监控时创建后续检查任务。

## 旧宿主：手动阻塞交接

本 skill 是管理端操作手册。仅在宿主不支持原生强制监管、且用户要求分阶段监督时，让对应 DeepSeek 任务加载项目提供的 `dsh-gpt-supervisor` skill，或使用项目桥接 CLI：`plan → checkpoint（必要时）→ acceptance`。`connector.native_supervision=true` 时由插件自动接管，不发送加载技能或手动交接的指令。

桥接命令必须在该会话的项目根目录，作为唯一前台调用，使用 `timeoutMs=600000`、`run_in_background=false`；等待期间不得并行开发。`DSH_SESSION_ID` 使用该任务真实上下文，不能冒用。总等待上限 540 秒，`blocked` 或暂停证据失败都不能当作批准。新的复审使用新 request ID；相同 ID 返回已有缓存。


## 多审查器设置

设置页可选择统一配置或按 plan/checkpoint/acceptance 分阶段配置，审查器支持 Codex/GPT、Claude CLI、DeepSeek Harness。模型从工具目录下拉选择，刷新失败时保留缓存并明确显示错误，不自动改选模型。每个已入队审批保存实际审查器和模型，改设置不重跑历史任务。

Harness 审查必须使用独立 base + headless profile，模型和凭据沿用指定 home；专用进程禁用自动监管与连接插件，避免递归。安装方法见项目 README 的“多审查器与模型”。Claude 模型目录通过项目依赖 Agent SDK 连接本机 CLI 查询，不发送生成请求。升级复制完整 scripts，并保留指向已安装依赖的 reviewer_helper 路径。

`GET /reviewers/<provider>/models` 读取目录；`POST /reviewers/<provider>/models/refresh` 刷新目录，沿用管理鉴权和操作 UUID。目录查询不消耗审查次数；真实验收按原额度计数。历史记录没有模型快照时显示“未记录”，不可按当前设置推断历史使用模型。
