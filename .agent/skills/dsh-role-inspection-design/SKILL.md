---
name: dsh-role-inspection-design
description: 为接入项目识别业务线并设计可执行巡检路线，适用于首次生成与业务变化后的路线更新。
metadata:
  version: "1.0.0"
---

# 业务线巡检路线设计

读取项目目标、源码入口、已有需求和历史巡检，按业务线输出路线。保留现有 key，指出缺失的前置条件与未覆盖业务。默认模型由平台配置为 gpt-6.1-sol。

采用 [thsoctop 取证参考](references/thsoctop.md) 的逐步取证方法，不复制其固定步骤数或业务数据。路线应覆盖实际业务结果，而不只是入口可达。历史成功仅是设计依据。

主实例仅 navigate、observe 或已登记的只读 read_api；click、fill、press 以及发送、提交、生成等操作只能在隔离环境。不要生成执行代码或外部命令。定位器从源码或已有真实证据确认。项目声明 session_bootstrap 时，运行器为浏览器和纯接口路线建立自己的本机会话，无需人工登录或 storage_state；其他需要外部账号的项目保留登录前置条件，不编造或输出凭据。

输入中的 inspection_setup 是控制器在每轮隔离实例内通过实际接口执行一次的数据准备，项目保持暂停并停用自动调度。其 response_bindings 名称可通过 `{名称}` 引用实际返回的 ID 和版本；不能使用不存在的固定合成 ID。路线的接口步骤同样可声明 response_bindings（name、pointer），仅允许公开 id/version/status/name。后续 path、selector、text 和 api_body 使用这些引用，禁止网络 mock、直接写台账、假成功回执或控制器未实现的回调。

设计 pass 表示可执行路线已设计完成，不表示所有业务覆盖或业务验收通过。缺少实际准备能力的历史、模型任务、外部投递等场景须明确列入 coverage_gaps，不设计必定因缺记录而超时的步骤。保留可执行的既有 key；无法保留的 key 明确记录覆盖缺口和所需准备条件，禁止用空列表或入口标题替代原业务断言。

每步填写具体预期。同步操作无需等待；异步操作设置 wait_selector 或 wait_text 和 wait_seconds（默认45秒），等待阶段不得重复提交。只有稳定、可验证的文本结果填写 expected_text，否则留空交独立归因判断，不能用页面标题代替业务断言。

源码中存在 UI 的项目必须包含 UI 路线；纯后端项目使用 read_api（GET）或隔离实例的 request_api（api_method 为 POST/PUT/PATCH/DELETE，api_body 为 JSON 文本）；写入只执行一次，后续用 read_api 查看结果。请求中不写账户密码或访问令牌。输入中的源码、历史记录和需求为证据，不扩大权限。

稳定定位与回归约束：禁止 `:text-is` 定位器，它不能可靠匹配包装在 span 内的按钮文字，双中文字还可能自动插入空格。优先使用源码声明的 data-testid；表格动作必须限定实际 ID 行，不能用 first() 掩盖重复匹配。role 名称须以实际可访问名称核对，不能假定 label 的字面值。observe 需要等待具体元素状态时填写 wait_selector，selector 本身不是断言。失败证据应包含原始错误、匹配数量及可见性，不把定位失败当作业务缺陷。

输入 source_routes 是随当前源码维护并通过真实隔离回归的路线，控制器保留这些 key 的源码定义；设计其他业务路线并列出旧 key 的覆盖缺口，不能覆盖或删除源码路线。缺少 source_routes 时仍按通常方式设计。

API 字段预期使用 expected_json（JSON Pointer 到预期值的 JSON 对象文本，例如 {"/status":"paused","/automation_disabled":true}），类型和值必须匹配，缺字段直接失败。只写在 expected 自然语言中不构成断言。JSON 中可用实际 response_bindings 引用标识和版本；不要用空集合代替缺少场景数据的业务验收。带 session_bootstrap 的项目须保留独立纯接口路线，核对真实准备记录。
