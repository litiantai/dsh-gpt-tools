# 管理接口与操作语义

脚本路径相对于当前 skill：`scripts/control.py`。路径参数不加 `/api` 前缀，脚本负责鉴权与前缀。读取返回 JSON；写操作需 `--operation-id UUID`，正文通过 UTF-8 JSON 文件提供。

## 读取

| 路径 | 用途 |
| --- | --- |
| `/overview` | 服务、连接插件、额度和审查状态汇总 |
| `/sessions` | 发现的会话列表，标题、cwd、审批方式、subagent 标记 |
| `/sessions/<id>` | 会话详情、最近事件、人工指令记录 |
| `/reviews` | 最近审查及所选目录中的历史结果 |
| `/reviews/<id>` | 请求、状态、version、模型建议、人工结论、暂停证据和日志尾部 |
| `/events` | 最近 500 条操作事件与历史日志 |
| `/settings` | 实际 home/state/history_dir、模型、端口和插件心跳 |

## 写操作

所有 POST/PUT 均需稳定的操作 UUID。`control.py` 按路径自动选择方法；请求正文不能包含 `operation_id`，它由命令参数注入。

| 路径 | 正文示例 | 行为 |
| --- | --- | --- |
| `/service/start` | `{}` | 启动新版监工，不自动开始审查 |
| `/service/stop` | `{}` | 停止监工，取消活动审查并返回 blocked；管理页面继续运行 |
| `/sessions/<id>/approval-mode` | `{"mode":"manual"}` 或 `{"mode":"auto"}` | PUT，影响后续交接 |
| `/sessions/<id>/messages` | `{"text":"请核对边界条件，再运行相关测试。"}` | 向指定会话即时插话 |
| `/sessions/<id>/stop` | `{}` | 停止目标会话当前轮次 |
| `/reviews` | `{"session_id":"…","phase":"checkpoint","scope":["src"],"summary":"核对错误处理"}` | 发起观察审查，会计入额度 |
| `/reviews/<id>/retry` | `{}` | 新建关联的观察审查，不恢复 DeepSeek |
| `/reviews/<id>/cancel` | `{}` | 取消尚未结束的审查 |
| `/reviews/<id>/takeover` | `{"version":3}` | 在结果返回前接管阻塞交接 |
| `/reviews/<id>/decision` | `{"version":5,"decision":"revise","instruction":"修复空输入问题后重新验收。"}` | 提交人工结论，并再次核对暂停证据 |
| `/settings` | `{"model":"用户选择的模型","review_timeout":240}` | PUT，只传需要更改的字段 |

审批规则：

- 只有 `mode=handoff` 的活动审查可接管；`awaiting_human` 状态才可提交人工结论。
- `plan/checkpoint` 支持 `approve`、`revise`；`acceptance` 支持 `done`、`revise`。
- 必须填写明确的 instruction。暂停证据失败仍返回 blocked，人工按钮不能绕过。
- 自动模式下模型结果可能先于接管提交返回；409 表示需要重新核对，不能伪造新 version 覆盖终态。

## 送达与故障

`commands` 中的状态：pending（提交中）、dispatching（正在送达）、accepted（Harness 已接收）、consumed（会话已消费）、failed（失败）、unknown（待核实）。Consumed 通过会话 user/message 的源 rpcId 精确匹配，不按相同文本猜测。

若目标正在等待交接，管理服务先取消审查，等待 blocked 响应返回且审查进程停止，再发送插话。未确认原交接送达时操作报错，不会发送新的指令。

- 插件离线：检查设置页心跳；不要绕过鉴权向不明 Harness 端口发消息。
- 目录不匹配：读取实际 home/state，停止监工并等待当前操作结束后，按用户目标修正配置。
- 请求超时或 5xx：操作可能已发生，先查目标记录；不能用新 ID 强制再次执行。
- 同一操作 ID 内容不同：409，属于客户端冲突。
- 额度耗尽：报告剩余额度与日期，不删 quota.json。
- 模型不可用：核对用户配置与该次 stderr.log，不扫描或输出全局认证文件。
- 网络错误：不会伪装为审批通过或消息已消费。

## CLI 与状态目录

管理服务默认 13084；新版交接桥默认 13083。项目根目录的旧兼容入口 `dsh_bridge.py` 默认 13081，不能与新版参数混用。

设置页 `state_dir` 是管理数据库和当前证据所在位置；`history_dir` 只是历史读取来源。更改 home/bridge_port 前先停止监工并等待操作结束，保留已有额度与证据。显示时间为 Asia/Shanghai。
