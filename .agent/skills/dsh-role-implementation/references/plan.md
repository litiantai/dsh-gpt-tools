只读分析项目与验收条件，返回可实施的 plan。

当 record.repair_plan_required=true 时，本轮必须重新制定技术修复方案。读取 feedback、last_revision_diagnostic、真实失败回执及 previous_plan，说明每项剩余缺陷的根因、涉及文件/接口、具体修改、对应回归检查、验证通过标准、与上一轮方案不同之处及风险。不得把 Watch 的调度方案直接当作技术修复方案，不得在计划阶段修改源码；提交 plan 后等待独立方案评审通过再开发。
