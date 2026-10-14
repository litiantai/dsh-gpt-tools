# 任务内容

标识与标题、需要实现的行为、涉及模块、上游文档版本、依赖任务、验收条件与检查方法。

并行只用于独立修改与工作区；同一个接口契约先冻结再分工。发现新范围时形成变更草稿，不悄悄塞入当前任务。

离线校验脚本读取 {tasks, required_acceptance_ids}。每个 task 的 id/scope/verification 为非空文本，depends_on 与 acceptance_ids 为文本列表。required_acceptance_ids 是本次需求的验收标识列表；这份检查输入仅用于验证拆解，不新增平台运行契约或改变任务状态。
