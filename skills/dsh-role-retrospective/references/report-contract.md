# 报告与需求转化契约

## 两种调用入口

交互技能使用本技能目录下的以下 CLI；先通过 `--help` 核对当前版本。`<skill-dir>` 是本次读取技能的目录。`--date` 是北京时间统计自然日；`--state-dir` 必须来自当前项目的真实配置，不能猜测或换用其他运行实例。

```text
python3 <skill-dir>/scripts/retrospective.py collect --state-dir <state> --product-id <id> --date YYYY-MM-DD --output <evidence.json> [--origin <localhttp>]
python3 <skill-dir>/scripts/retrospective.py render --evidence <evidence.json> [--analysis <analysis.json>] [--conversion <conversion-receipt.json>] --output <report.html>
python3 <skill-dir>/scripts/retrospective.py apply --evidence <evidence.json> --analysis <analysis.json> --origin <localhttp> --output <conversion-receipt.json>
```

collect 和 render 不触发模型执行、邮件发送或部署。模型阅读收集产物后按 schema 写 analysis.json；省略 analysis 时只能生成事实报告，不能暗示已完成独立归因。apply 是明确的写操作：用户已授权入池时执行，无需本技能重复确认，控制器校验后调用现有需求接口。保留转换回执并将真实结果体现在报告中。脚本或证据不可用时保留结构化结论并报告具体缺口，不假称已生成或已入池。

collect 可同时提供 `--start` 和 `--end` 有时区 ISO 时间，表示 `[start,end)` 的明确调度窗口；默认仍为 `--date` 北京时间自然日。不得把晚间 19 点切分的调度窗口标成完整自然日。完整任务清单包括本项目全部任务，以及每项在窗口内的可靠回执和当前只读进度；旧列表的 1000 条上限不适用于采集。

平台阶段由调度器提供结构化输入，模型仅返回 schema JSON。控制器另行负责收集、HTML 渲染和候选入池；不能因为技能或 schema 已更新就宣称自动流程已接通。

平台控制器入口为 `autopilot.retrospective_report.prepare` 和 `finish`。prepare 冻结完整脱敏 evidence.json 与 SHA-256；finish 校验项目、窗口和已保存哈希，写 analysis.json、conversion-receipt.json、report.html。智能归因失败时仍生成完整事实报告，明确失败原因；早七邮件可发送该事实快照，同时保留异步归因和需求转换状态。邮件附件使用不可变副本与独立哈希。

## 证据与结构化结论

输入应包含项目 ID、北京时间统计窗口、快照时间、来源覆盖情况，以及 products/runs、inspections/inspection_rounds/evidence、signals、reviews、code_reviews/deliveries/releases、requirements 与当前队列的结构化记录。关联只使用显式 ID、call_id、验收条件与提交身份，不依据同项目、相似时间或摘要猜测。现有 RunProgress.current、delivery_board.pending_issues 和 record_context 可用于本地投影；报告生成不需要刷新外部 PR 或执行测试来制造历史证据。

输出保留原 status、reason、summary、accomplishments、problems、lessons、next_actions 七项；新字段均为数组，缺省可为空以兼容旧回执：

- coverage：每个 source_kind 的 complete、record_ids 与 limitations。完整性必须继承实际采集情况，不能由模型补成完整。
- problem_items：source_kind/source_id、title、cause、problem_type/failure_kind、evidence/evidence_ids、signal_ids、requirement_ids、window_status/current_status、disposition 与 next_action。window_status 表示窗口内新发生、持续、恢复、未观察或历史不完整；current_status 保留当前真实状态。
- requirement_candidates：title、signal_ids、existing_requirement_id、evidence、impact、reproduction、acceptance、classification、in_scope、priority、resolution_probes。没有真实 signal_ids 或可核对证据时只列问题和补证行动；已有需求命中后复用，把本轮新增证据保留在报告与转换回执中，不能仅因标题变化另建重复需求。

classification 使用 development/investigation/environment。development 需要对原问题有效的探针与验收；investigation 需要具体调查目标与复验条件；environment 说明环境障碍及恢复条件。常规额度、低峰和资源等待不创建开发需求。候选入池前校验项目归属、证据引用、输入版本、用户授权和既有范围；重放同一输入复用同一来源，不重新造信号或需求。

自动入池以明确已有需求 ID、规范化标题和本项目真实信号为依据；稳定指纹包含项目、规范化标题和信号集合。同一信号可对应多个不同改进，不能把关联信号相同当作同因证明。已有关联但标题变化且没有明确已有需求 ID 时，记录歧义和建议关联 ID，补证后再转化；不能任意合并到第一条需求。候选只允许声明字段，不能携带 call、executor 或运行状态覆盖控制器。开发探针无效时降级调查，保留原需求确认门槛；入池后的开发沿原任务队列推进，调查与环境需求沿原调查、额度与低峰规则推进。

HTML 至少呈现项目和时间窗口、覆盖缺口、窗口内任务与问题、当前卡点、恢复事实、关联需求进展、待办和优化建议。保留完整标题与原因，记录 ID 用次级样式；“无”仅适用于已完整覆盖且没有对应事实的区域。控制器渲染前后的结构化快照与结果应保存在同一产物目录，便于核对事实与重复执行。动态内容转义，不回显凭据或未知数据键。

交互 apply 使用现有管理 API 和稳定操作 UUID。优先复用显式已有需求 ID、关联信号及相同标题；P0 待处理信号使用平台现有手动转需求接口，其他候选通过普通需求入池接口登记，来源保存在证据文本与报告中。默认仅入池，不触发模型执行；用户已经授权执行且候选满足原门槛时可使用 `--queue` 进入原任务队列。平台阶段沿用 Scheduler.accept_discovery 和 intake 的原子校验与确认规则。脚本输出实际新增/复用/跳过/失败及关联需求 ID，不能把候选数量当成已入池数量。关联原任务的返修不另造任务，未知历史和未完整归因批次保留待核对。
