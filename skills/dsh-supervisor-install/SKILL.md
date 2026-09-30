---
name: dsh-supervisor-install
description: 安装、升级或修复 dsh-gpt-tools 的 DeepSeek ↔ GPT 监工工作台，构建 React 看板，配置 Python 管理服务、审查桥与 Harness 连接插件。用于首次部署、旧版切换或连接配置，不用于日常审查与发送会话指令。
---

# 安装监工工作台

将用户指定的 dsh-gpt-tools 项目部署为可访问的本机看板，并核对审查桥与目标 Harness 实例的连接。只安装用户请求的部分；“给我安装步骤”仅输出步骤。

## 定位与检查

1. 从当前工作目录、用户提供路径或 `DSH_GPT_TOOLS_ROOT` 定位项目。用 `package.json`、`dashboard_server.py`、`connector/package.json` 确认身份；不要假定 skill 目录就是项目目录，也不要编造仓库下载地址。均不可用时询问源码路径。
2. 阅读项目 `README.md` 的启动、旧版服务、连接插件章节。详细顺序见 [安装流程](references/install.md)。
3. 检查 Node.js 22.12+、npm、Python 3.10+；真实审查需要 Git、zstd、已登录的 Codex CLI；Harness 插件安装需要目标实例对应的 dsh CLI 与 pnpm。优先使用已有工具；Codex 登录通过正常交互完成，不读取认证文件。
4. 确认四个实际值：目标 Harness home、管理状态目录、管理端口、桥接端口。默认 home 为 `DSH_HOME` 或 `~/.dsh`，状态目录为 `DSH_SUPERVISOR_STATE` 或 `<home>/supervisor`，端口为 13084 / 13083。检查 dsh 启动器和 profile 元数据辨认实例，不输出整个配置中的密钥。

## 执行

- 有锁文件时运行 `npm ci`，随后 `npm run build`；失败时修复具体依赖或构建问题，不删除用户源码或已有状态。
- 复用匹配的管理服务。未启动时用项目 `dashboard_server.py` 与已确认的状态目录启动，将 PID 和日志留在可定位的位置；仅绑定回环地址。
- 先检查服务状态，再启动新版审查桥。旧版占用时核对 PID 的命令、状态目录和活动审查。用户已要求升级且原实例已确认空闲时可以切换；无法确认实例或仍有任务时说明具体情况，保留原服务，询问缺失的目标或中断意图。不要清空额度、覆盖审查结果或并行启动两个桥。
- 在目标 Harness 的实际 profile 安装项目 `connector` 包。复用已有插件项；配置 `stateDir`、`home`、`origin` 与管理服务一致。只调整该插件配置，不重写整份 profile。
- 独立安装阻塞式协作 skill 时复制项目整个 `dsh-gpt-supervisor` 目录，包含 `bridge.py`、`review_core.py` 和 references。目标为用户所选 Harness 的 `<home>/skills/dsh-gpt-supervisor`；这是让 DeepSeek 进入交接协议的 skill，与 Host 连接插件用途不同。

## 验收与交付

核对页面可打开、新桥 `protocol=dashboard-v1`、`connector.online=true`、`connector.home_matches=true`。读管理 API 时先取得本地 Cookie/CSRF；可使用“使用监工工作台”skill 的脚本（如已安装），也可按项目 API 自行实现，不能把它当作必要依赖。

安装过程不通过发送真实会话消息或运行收费审查来冒充连接检查。用户明确要求联调时，选定独立测试会话再执行。

报告看板 URL、项目/home/state/profile 的实际位置、启动方式和检查结果。若插件未加载、旧服务未切换或用户尚未登录，逐项标为未完成，不宣称整个安装已就绪。
