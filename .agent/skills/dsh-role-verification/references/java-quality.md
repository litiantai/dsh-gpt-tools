# Java / Spring Boot 质量标准

先使用项目声明的 Maven 或 Gradle 命令编译，再执行测试；不能通过跳过编译或以文字说明替代真实回执。缺少 JDK、依赖、命令或隔离环境应记录阻塞。测试失败应记录失败。

代码评审必须核对本次新增或修改的 Spring Boot 类及方法：
- 类使用准确解释用途的中文 JavaDoc。
- 未标注 @Override 的方法使用准确解释用途的中文 JavaDoc，按需包含 @param、@return、@throws。
- 上述 JavaDoc 包含 @author 李天泰 <litiantai@myhexin.com>。
- 上述 JavaDoc 包含编写当天的 @date，格式 yyyy/MM/dd，以 Asia/Shanghai 日期为准。复审旧提交时按实际编写日核对，不强制重写为复审日。
- @Override 方法可继承接口或父类文档，无需重复编写。
- 只检查本次新增或修改的相关代码，不自动整改未改动的历史源码。
