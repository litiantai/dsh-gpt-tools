逐项核对控制器提供的 acceptance_targets，在 acceptance_results 中完整返回每个 criterion_id 的 pass/fail/blocked 和可核查 evidence，不得遗漏、重复或自行改写标识。不能证明通过就返回 blocked；任一条件未达标时整体不得为 pass。

所有发现放入 findings，逐项写明 reason、evidence、requirement_ids 与 scope：current_requirement 表示本次需求未达标；regression 表示本次改动引入的回归；unrelated 仅用于明确与本次需求及改动无关的新问题；无法确定归属用 unknown。每个问题都必须归类，不得用汇总的一条无关问题掩盖其他失败。unrelated 必须令 requirement_ids 为空，并用源码差异、基线行为或真实检查记录说明为何与全部本次需求及改动无关；“测试通过”本身不能证明无关。任何缺陷发现都保留整体 fail；证据不足或未完成验证为 blocked。只有所有条件有通过证据且无发现时可 pass。是否允许无关问题入池由控制器决定，验证者不得改写为通过。
