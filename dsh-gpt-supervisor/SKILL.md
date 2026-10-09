---
name: dsh-gpt-supervisor
description: 让 DeepSeek 在方案确认、关键问题和最终验收时唤醒 Codex/GPT 监工，阻塞等待审查结果，再按返回指令恢复工作。用于用户要求 GPT 监督或双向交接的开发任务。
---

# GPT 双向监工

把当前用户任务交给 GPT 分阶段监督。轻量本机服务负责传递请求和结果；等待审查时 DeepSeek 的模型循环停在前台工具调用。此 skill 生效于加载它的任务，安装本身不会强制托管所有会话。

## 与新版插件的关系

如果看板显示“插件强制监管已启用”，方案、异常和验收已由插件钩子自动交接，无需加载本 skill 或重复执行桥接 CLI。遵循插件注入的监管指令；本 skill 下述命令仅用于未启用原生强制监管的任务。

## 初始化

`<skill-dir>` 是加载本 skill 时返回的资源基目录，使用其绝对路径，给含空格的路径加引号。脚本为 `scripts/bridge.py`，不必为执行它读完整源码。

先以普通前台 Bash 执行一次：

```sh
python3 -B "<skill-dir>/scripts/bridge.py" start
```

已运行时复用服务；首次启动创建后台传递服务，**服务自身不调用 GPT**。需要本机 Python 3、Git、zstd、已安装且登录的 Codex CLI。默认端口 13083、审查模型 gpt-5.5、low 推理等级；默认状态目录为 `$DSH_HOME/supervisor`（未设置 DSH_HOME 时为 `~/.dsh/supervisor`）。故障只读取 [运行说明](references/operations.md)，不要扫描全局凭据或修改用户全局配置。

如果启动失败，报告缺少的依赖或具体错误，停止受监督任务，不能跳过监督继续实现。

## 请求与暂停

方案确定、尚未修改实现前，先做 `plan` 交接。把原始任务目标、授权的改动范围、具体方案、验收标准写入任务工作目录的一份摘要文件。保持简短，通常 1000～2500 字以内；不加入凭据、无关会话或大段日志。

从**当前会话的项目根目录**执行：

```sh
python3 -B "<skill-dir>/scripts/bridge.py" handoff --phase plan --scope src --scope test --summary-file "/绝对路径/plan.md"
```

`--scope` 按本任务实际授权路径填写，可重复；范围是项目内相对路径。上面的 src/test 仅为示例。未指定时审查整个工作目录。摘要应放在所选源码范围外；不要为监工扩大用户授权的改动范围。Harness 自动传入 `DSH_SESSION_ID`，不要冒用其他会话 ID。

每次交接必须是**唯一的前台 Bash 调用**，参数 `timeoutMs=600000`、`run_in_background=false`。在交接前结束本任务正在写文件的后台工作；等待期间不得并行调用工具、启动子代理或继续开发。若前台命令被提升为后台任务，只能使用 `job_output(wait=true)` 等待结果。

实现期间只在具体阻塞、持续失败、目标或方案发生实质变化时以 `--phase checkpoint` 交接；正常每一步不唤醒 GPT。完成实现并执行必要验证后，写入文件清单、真实测试结果、已知限制，以 `--phase acceptance` 交接。返修重用同样的范围，并补充上一轮 issues 的修复证据。

## 接收与恢复

Bash 返回的 JSON 就是 GPT 发给 DeepSeek 的恢复通知，必须读取 `decision`、`instruction`、`issues` 和 `pause_proof`：

- `approve`：明确说明已收到 GPT 结果，按 instruction 开始或继续工作。
- `revise`：恢复工作，只修复提出的问题；完成后重新交接相应阶段。
- `done`：验收通过，向用户报告产出和证据，结束当前任务。
- `blocked`、非零退出或通信失败：停止工作并报告原因。审查器忙时保持停止、用同一 request_id 稍后重试；其余故障修复后才重试。不能把故障当成批准。

`pause_proof.pause_verified=false` 代表审查期间有新步骤、工具调用或被监控文件变化，不能声称暂停成功。检查具体证据；审查器执行测试若修改了纳入范围的文件，也需要解释和处理。

同一次 HTTP 请求保持阻塞，审查完成后的响应直接进入当前 Bash 的工具结果，无需页面轮询或另发恢复消息。默认每次审查最多 240 秒、每天最多 12 次；同一 `--request-id UUID` 重试使用缓存。修复同一问题最多连续复验 3 次，仍未解决则停止并报告，不无限循环。

## 验收证据

使用任务相关的真实测试、构建或浏览器验证，不用自述代替证据。区分逻辑单测、界面实测和部署状态。最后报告真实的阻塞交接次数；通过页面或对话收到的额外 GPT 纠偏应单列，不能算作一次已执行的阻塞交接。


## 多审查器设置

设置页可选择统一配置或按 plan/checkpoint/acceptance 分阶段配置，审查器支持 Codex/GPT、Claude CLI、DeepSeek Harness。模型从工具目录下拉选择，刷新失败时保留缓存并明确显示错误，不自动改选模型。每个已入队审批保存实际审查器和模型，改设置不重跑历史任务。

Harness 审查必须使用独立 base + headless profile，模型和凭据沿用指定 home；专用进程禁用自动监管与连接插件，避免递归。安装方法见项目 README 的“多审查器与模型”。Claude 模型目录通过项目依赖 Agent SDK 连接本机 CLI 查询，不发送生成请求。升级复制完整 scripts，并保留指向已安装依赖的 reviewer_helper 路径。

`GET /reviewers/<provider>/models` 读取目录；`POST /reviewers/<provider>/models/refresh` 刷新目录，沿用管理鉴权和操作 UUID。目录查询不消耗审查次数；真实验收按原额度计数。历史记录没有模型快照时显示“未记录”，不可按当前设置推断历史使用模型。
