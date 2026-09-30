# 双向监工真实测试报告

2026-09-30，Asia/Shanghai。项目：`/Users/ranrui/Desktop/thsoctop`。
DeepSeek 会话：`session-1903f8d1-0671-4478-8752-428b8d16979e`，标题「贪吃蛇小游戏集成与双向监工」。

## 结论

新增贪吃蛇完成。真实链路已经验证：DeepSeek 前台 Bash 调用阻塞交接 → 唤醒本机 Codex CLI/GPT → GPT 返回 JSON 指令 → 同一次 Bash 的工具结果通知 DeepSeek → DeepSeek 恢复执行。最终 GPT 给出 `done`。

| 阶段 | 本地开始时间 | GPT 审查耗时 | 返回决策 | 审查期间新步骤 / 工具调用 | 游戏源文件未变 |
| --- | --- | --- | --- | --- | --- |
| plan | 11:33:39 | 9.49 秒 | blocked | 0 / 0 | 是 |
| plan | 11:36:37 | 42.84 秒 | approve | 0 / 0 | 是 |
| acceptance | 11:45:07 | 233.39 秒 | revise | 0 / 0 | 是 |
| acceptance | 11:57:21 | 59.4 秒 | done | 0 / 0 | 是 |

首次调用因 CLI 不支持桌面配置模型名返回 blocked；DeepSeek 停止，没有写实现。桥接服务改为本机已真实探测可用的 gpt-5.5，未修改用户全局配置。

方案获得 approve 后恢复实现；第一次验收返回 revise，指出 NaN 帧时间污染计时器，DeepSeek 自动恢复修复。我还通过 Computer Use 插话发送棋盘不随普通移动重绘的问题，DeepSeek 修复并重新验收。每次请求的 request_id、暂停证据、工具结果序号和后续恢复步骤序号保存在 `dsh-bidirectional-evidence.json`。

## 游戏验证

验收通过。已基于源码与本地命令核验 packages/dsh-games：贪吃蛇作为第三个 Tab 接入，前两轮指出的 NaN 时间归一化与棋盘重绘签名遗漏均已有针对性实现和单测覆盖。

最终 GPT 核验：

- 范围核对：git status --short packages/dsh-games 仅显示 packages/dsh-games 内 11 个变更/新增文件，符合本任务范围。
- 代码核对：snake-hud.ts 的 readHud/hudSignature 已纳入 steps、facing、foodKey、goldKey，覆盖普通移动、转向、食物变化、金苹果出现/消失的重绘触发。
- 代码核对：snake.ts 使用 safeDelta 处理 NaN、±Infinity、负数与超长帧，并统一驱动 elapsedMs、goldLeftMs、accMs。
- 测试通过：./node_modules/.bin/vitest run packages/dsh-games/test，4 个测试文件、101 条用例全部通过。
- 类型检查通过：./node_modules/.bin/tsc --noEmit -p packages/dsh-games/tsconfig.json 无输出，退出 0。
- 客户端类型检查通过：./node_modules/.bin/tsc --noEmit -p packages/dsh-games/tsconfig.client.json 无输出，退出 0。
- 构建通过：node scripts/build-plugin.mjs packages/dsh-games，client 产物 9 个输入模块，host 产物 lib/ 成功生成。

独立 Computer Use：实际第三个 Tab 可以启动，观察到蛇从初始位置移动到其他格；暂停按钮显示「已暂停」，继续后运行；切到五子棋再回来，仍为「已暂停」。真实主题 token 下配色正确。

DeepSeek 一次性真实 Chrome 验证日志另见验收资料：不同蛇身布局、暂停时固定位置、恢复后变化、切 Tab 自动暂停、重新开局食物变化。DOM 验证没有纳入长期自动化测试，未把这些一次性证据描述成持续回归保障。

源代码改动在 `packages/dsh-games`；完整改动另见 `thsoctop-snake.patch`。仓库范围外新增状态：[]。测试产生的项目 pnpm 缓存已移至本任务 work 目录保留。未部署到已安装桌面运行时，预览使用本次源码构建。

## 继续使用

桥接服务运行于 `127.0.0.1:13081`，观察到 87 个会话。轻量扫描不调用模型；只有显式交接触发 GPT。默认 low 推理等级，每次审查240秒、每天最多12次；同 request_id 重试复用结果；失败返回 blocked。

此版本通过任务约定实现暂停：单次前台阻塞调用、不并行或后台开发。会话发现覆盖所有会话，只有接入交接协议的任务会暂停并接受 GPT 监督；尚未给 Harness 全局植入强制交接。服务不自动开机启动。本次用户监督操作已通过 Computer Use 实测。
