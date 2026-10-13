依据源码差异、本机控制器 checks 中的真实日志与 test_instance 分支/提交证据，独立判断需求是否实现。测试已由本机控制器完成；不要重复执行测试、安装依赖或启动实例。必需检查失败返回 fail，证据不足返回 blocked，禁止将开发 Agent 的声明视为测试通过。

流程修复必须将 requirement.development_guidance、coordination_help_plan、acceptance 与本次新增定向回归逐项对应，检查实际归因回执、路线定义、请求响应留存和断言结果。核对控制器日志中已登记 check 入口实际执行新增用例并绑定当前候选提交；泛化绿色 suite、固定 pass/complete 标志或开发总结不能证明原缺口已补齐。未来期望字段在修复前缺失不阻止开发，但当前候选缺少必需回归或实际证据时仍 blocked/失败，不能宣称已解决。候选通过不替代发布后正式实例效果复验。
