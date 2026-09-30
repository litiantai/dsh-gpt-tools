# 验收：packages/dsh-games 新增「贪吃蛇」（第三个 Tab）

- 工作目录：`/Users/ranrui/Desktop/thsoctop`
- 阶段：acceptance（**第 3 轮**：第 1 轮 `revise` 修 NaN 归一化；第 2 轮 `revise` 修棋盘不重绘）
- 范围：**仅 `packages/dsh-games`**；未提交、未推送、未部署
- 方案：`plan.md`（GPT 判定 `approve`，request_id `95762be5-215e-4b14-841a-78d07378bf7f`）
- 第 1 轮验收：request_id `18fca54f-…` → `revise`（NaN 归一化）
- 第 2 轮验收：request_id 见上一轮 → `revise`（棋盘不随每一步重绘）

## 0-A. 第 2 轮 revise 的修复：棋盘不重绘（本轮变更，**上线前必修**）

监工在真实 SVG 页面上发现：`syncHud` / `hudSignature` 只包含阶段/分数/蛇长/难度等 HUD 字段，
**不含 `steps`、方向、食物位置**。React 只在 state 变化时重渲染，而棋盘是从可变 `world` 引用上读的，
所以「普通移动一格」「重开换了食物位置」「转向」「金苹果出现或消失」这些不改变 HUD 的情况都不会
`setState` —— SVG 会停在原地不动。这不是性能优化，是棋盘根本不动。

复现与修复（`src/client/snake-hud.ts` 新增，纯数据、零 import）：

```ts
export function readHud(world: World, high: number): Hud {
  return {
    phase, score, length, difficulty, high, progress, eaten, reason,
    steps: world.steps,          // 覆盖「走了一步」
    facing: facingOf(world),     // 覆盖「已按键、还没走」的转向
    foodKey: cellKey(world.food),// 覆盖「重开/吃到后换了食物位置」
    goldKey: cellKey(world.gold),// 覆盖「金苹果出现或超时消失」
  }
}
export function hudSignature(hud: Hud): string { /* …以上字段全部参与… */ }
```

同时修掉一个连带问题：引擎要等这一步走完才改 `world.dir`（走之前改会在移动前就判定撞死），
所以直接拿 `world.dir` 画面部朝会导致「按了方向键、眼睛不动」。新增纯函数 `facingOf(world)`
= 输入缓冲里最后一个待生效方向（没有则当前方向），**渲染朝向立刻跟上按键，而移动仍然按引擎节奏**。

**为什么单独拆一个 `snake-hud.ts`**：这段「什么时候需要重绘」的判据必须能被单测锁住，而视图文件
`import react`，本包测试跑在 node 环境、没有 react 依赖 —— 逻辑留在视图里就测不到，
这恰恰是当初漏掉这个 bug 的原因（第一次尝试把 `readHud` 放视图里直接导致整个测试文件无法加载）。

## 0-B. 第 1 轮 revise 的修复：`tick(dtMs)` 时间归一化

GPT 指出的问题成立：注释写着「负数与 NaN 视为 0」，实现却是 `Math.max(0, dtMs)`
—— **`Math.max(0, NaN)` 就是 `NaN`**，于是 `world.accMs += dtMs` 把 accMs 污染成 NaN，
`accMs >= stepMs` 从此恒为 false，游戏静默卡死；`goldLeftMs` 同样会被污染。
原先测试只断言 `steps === 0`，没发现状态已损坏。

```ts
export const MAX_FRAME_MS = 1000
function safeDelta(dtMs: number): number {
  if (!Number.isFinite(dtMs)) return 0   // NaN / ±Infinity
  if (dtMs <= 0) return 0                // 负数与 0
  return Math.min(dtMs, MAX_FRAME_MS)    // 超长帧截断
}
```

`tick` 现在用 `safeDt` 驱动 elapsedMs / goldLeftMs / accMs 三处，注释承诺的行为与实现一致，
并补上了此前注释提过、实现却没做的 0~1000ms 截断。视图侧删掉了自己那份 250ms 局部截断常量，
时间归一化只由引擎一处负责。

守卫有效性做了反证（临时测试，已删除）：旧写法 `accMs + Math.max(0, NaN)` 得到 `NaN`，
`NaN >= stepMs` 为 `false`（永远不再推进）；新实现 `accMs` 保持 0 且下一帧正常走 1 步。

## 1. 本次实现了什么

贪吃蛇作为第三个 Tab 接入现有 `ths-octop-games` 面板，五子棋与打飞机未改动其逻辑。

- 纯逻辑引擎 `src/client/snake.ts`：整局状态是一个可变世界对象，`tick(world, dtMs, rng)` 推进；
  规则、难度、投食、金苹果、胜负判定全部是纯函数，可注入 grid 与 rng。
- 视图 `src/client/snake-view.tsx`：SVG 网格（`<pattern>` 画格）+ rAF 循环，只有 HUD 摘要变化才 setState。
- 键盘（方向键 / WASD / 空格 / P / Esc / Enter / R）**挂在游戏舞台元素上**（`tabIndex=0`），
  不是 `window`，所以只在游戏区域有焦点时生效；方向键的滚屏默认行为被 `preventDefault` 拦掉。
- 屏幕方向按钮：3×3 网格 D-pad（上/左/右/下），`aria-label`，`playing` 之外自动禁用。
- 自动暂停三条链路：`hidden` prop（切 Tab）+ `document.visibilitychange`（切窗口）+ `IntersectionObserver`（滚出视口）。
- 结束：撞墙（悠闲档穿墙则不死）、咬到自己；胜利：分数达标、或把整个盘面吃满。
- 三档难度 悠闲 / 标准 / 大师：盘面 15×15 / 17×17 / 19×19，步进 200 / 150 / 105 ms 起、越吃越快，
  只有悠闲档穿墙，金苹果概率 0.5 / 0.4 / 0.32，分数目标 200 / 300 / 450。

未使用网络、未新增依赖、未写 localStorage（最高分与打飞机一致，只存在会话内存里）。

## 2. 文件清单（`git status --short packages/dsh-games` 的原始输出）

```
 M packages/dsh-games/README.md
 M packages/dsh-games/package.json
 M packages/dsh-games/src/client/index.tsx
 M packages/dsh-games/src/client/styles.ts
 M packages/dsh-games/src/index.ts
 M packages/dsh-games/test/host.test.ts
?? packages/dsh-games/src/client/snake-hud.ts
?? packages/dsh-games/src/client/snake-view.tsx
?? packages/dsh-games/src/client/snake.ts
?? packages/dsh-games/test/snake.test.ts
?? packages/dsh-games/tsconfig.client.json
```

| 文件 | 改动 |
| --- | --- |
| `src/client/snake.ts` | **新增**。贪吃蛇引擎（`World` / `tick` / `turn` / `beginRun` / `setDifficulty` / `recordHighScore` / `safeDelta` 等），无 DOM 依赖 |
| `src/client/snake-hud.ts` | **新增**。渲染摘要与签名（`readHud` / `hudSignature` / `facingOf` / `cellKey` / 文案表）；**零 import 除引擎类型**，所以能在 node 测试里直接锁住「什么时候该重绘」 |
| `src/client/snake-view.tsx` | **新增**。SVG 棋盘视图、键盘与 D-pad 输入、三重自动暂停、覆盖层、HUD |
| `test/snake.test.ts` | **新增**。50 条引擎 + 重绘触发单测 |
| `tsconfig.client.json` | **新增**。客户端半区类型检查配置（照 `packages/dsh-research/tsconfig.client.json` 先例） |
| `src/client/index.tsx` | `TABS` 增加 `{ key: 'snake', label: '贪吃蛇' }`，渲染 `<SnakePanel hidden={tab !== 'snake'} />` |
| `src/client/styles.ts` | 追加「贪吃蛇」CSS 段（棋盘、蛇身/蛇头/眼睛、食物、金苹果、进度条、D-pad、小屏媒体查询） |
| `src/index.ts` | `GamesStatus.games` 与返回值改为 `['gomoku', 'plane', 'snake']`；更新文件头注释 |
| `test/host.test.ts` | status 断言同步为三个游戏（**必要改动**：原断言硬编码两个游戏） |
| `package.json` | `typecheck` 改为 host + client 两条 tsc |
| `README.md` | 补「贪吃蛇」小节、接口清单改成三个游戏、typecheck 说明更正 |

`lib/` 是构建产物且被 `.gitignore` 忽略（`git check-ignore` 命中 `.gitignore:3:lib/`），所以不出现在 `git status` 里。

## 3. 命令与真实结果

### 3.1 单测

```
$ pnpm --filter @thsoctop/dsh-games run test

 ✓ test/plane.test.ts (16 tests) 5ms
 ✓ test/snake.test.ts (50 tests) 12ms
 ✓ test/host.test.ts (4 tests) 7ms
 ✓ test/gomoku.test.ts (31 tests) 50ms

 Test Files  4 passed (4)
      Tests  101 passed (101)
```

（第 1 轮 92 条 → 第 2 轮 95 条 → 本轮 101 条。）

新增 50 条覆盖：难度单调性与未知档回落、开局摆盘合法、food 不压蛇身、直行/转向、
反向与重复输入被拒、`MAX_QUEUED_TURNS=2` 上限、队首非法指令跳过、进食净长 +1 与提速、
金苹果 +30 且**不加长**、金苹果超时消失、按 `GOLD_EVERY` 掷骰（命中与不命中两条路径）、
撞墙（上/下/右边界）、穿墙、撞自己、追尾巴不误判、满盘胜利、分数目标胜利、胜利/结束后冻结、
暂停不计时、重开清空、换难度换盘面、攒够一步才动、`MAX_STEPS_PER_TICK=8` 超步保护、
负数 / NaN / ±Infinity / 超长帧的时间归一化与坏帧后可恢复、最高分按难度分桶，
以及本轮新增的**棋盘重绘触发** 6 条（见 3.1.1）。

#### 3.1.1 重绘触发专项（对应第 2 轮 revise）

这一组直接锁住「什么变化必须导致 setState」，逐条对应监工指出的场景：

| 用例 | 断言 |
| --- | --- |
| 普通移动一格就会换签名 | `steps` 变、签名变（**这就是棋盘原先不动的原因**） |
| 只按方向键、还没走的那一步也会换签名 | 引擎 `world.dir` 仍为 right，但渲染朝向 `facing` 立刻为 up，签名变 |
| 重开时即使分数与难度都一样，食物换位置也必须换签名 | 分数/难度/蛇长全相等，仅 `foodKey` 变 → 签名必须变 |
| 金苹果出现与超时消失都会换签名 | `goldKey` `'-'` → `'0,0'` → `'-'`，两次都换签名 |
| 金苹果存活时间超过单帧上限时需多帧才过期 | 截断仍然生效（`goldLeftMs` 精确剩 500） |
| 状态没变时签名稳定 | 避免无意义重渲染 |

#### 3.1.2 真实浏览器实测（第 2 轮 revise 的验证方法）

用真实 Chrome（154.0.8037.58，**有窗口**、`--remote-debugging-port`）加载一个临时 harness：
把真实 `SnakePanel` + 真实 `GAMES_CSS` 用 esbuild 打包进页面，复刻 `index.tsx` 的 Tab 结构，
再用 DevTools 协议读回 DOM 采样。harness 与截图都在 `/tmp/snake-verify/`，**不属于交付物**。

关键前提先说清楚：`--headless=new` 下 `requestAnimationFrame` **回调 0 次**（无合成器），
所以自动前进必须用有窗口的真实 Chrome 验证，否则测的是「rAF 被饿死」而不是游戏逻辑。
有窗口下测得 `1.5s 内 rAF 回调 10 次`（约 6.7fps，够驱动）。

**① 逐格移动（纯实时 rAF，未用探针）** —— 用 MutationObserver 记录 DOM 里 `data-head` 的真实变化：

```
16,8@8 -> 8,8@0 -> 9,8@1 -> 10,8@2 -> 11,8@3 -> 12,8@4 -> 13,8@5 -> 14,8@6 -> 15,8@7 -> 16,8@8
```

每一步只前进一格，`data-steps` 同步 +1，画面确实在跟着逻辑走（采样 6 次，6 个不同 head、6 种不同蛇身布局）。
另外确认 SVG 里渲染出的蛇身 `<rect>` 的 `data-cell` 与舞台 `data-head` 一致
（例：`head attr=16,8 x=16.06 y=8.06` / stage `data-head=16,8`），食物圆点 `data-cell=10,0` 也在画。

**② 暂停静止 / 恢复继续**：

```
暂停：head 16,8 -> 16,8（phase=paused，等待 700ms 不动）
恢复：head 16,8 -> 16,4（phase=playing，继续走）
```

**③ 切 Tab 自动暂停**（点 harness 的 plane 页签）：

```
切到「打飞机」：head 16,4 -> 16,4（phase=paused，等待 700ms 不动）
```

**④ 重开刷新食物位置**：

```
重开：food 5,16 -> 5,3，steps 12 -> 0，head 回到 8,8；重开后再移动 8,8 -> 11,8
```

**⑤ 方向键立刻改朝向**：按 `ArrowUp` 后 `facing=up` 而 head 未变（`16,8`），符合「所见即所得、移动仍按引擎节奏」。

**⑥ 主题配色的说明（避免误判成缺陷）**：本次 harness 页面**没有注入 `--dsw-alias-*` 主题 token**，
所以截图里棋盘背景/蛇身取到回退色（偏黑），食物红点是唯一的硬编码色所以可见。
监工已在 `http://127.0.0.1:13082/` 的真实第三个 Tab（带真实主题）独立复核：**配色正确**。
也就是说黑色是 harness 缺 token 的产物，**不是代码缺陷，也没有因此改动任何配色**。

### 3.2 类型检查（host + client 两个半区）

```
$ pnpm --filter @thsoctop/dsh-games run typecheck
$ tsc --noEmit -p tsconfig.json && tsc --noEmit -p tsconfig.client.json
（无输出）
typecheck exit=0
```

**这一步是本任务补的洞**：改动前 `tsconfig.json` 的 `include` 只有 `src/**/*.ts` 且 `exclude` 了
`src/client`，也就是说这个包的 `.tsx` 从来没被类型检查过。新配置一上来就抓到 6 个真实错误并已修复：

1. `snake-view.tsx` 里 `PAD` 同时用作 viewBox 留白常量与方向按钮表 → 重命名后者为 `DIR_PAD`（原本会编译失败/取到错误值）；
2. `snake.ts` 的 `World.grid` / `World.cellCount` 声明成 `readonly` 但 `reset()` 需要重新赋值 → 去掉这两个字段的 `readonly`（`GridSpec` 内部字段仍 readonly，且赋值对象仍 `Object.freeze`）。

现有 `gomoku-view.tsx` / `plane-view.tsx` 在这次新检查下**零报错**，未做任何修补。

### 3.3 构建

```
$ pnpm --filter @thsoctop/dsh-games run build
✓ @thsoctop/dsh-games: client 产物 lib/client.js（9 个输入模块）
✓ @thsoctop/dsh-games: host 产物 lib/
```

（第 1 轮为 8 个输入模块；本轮新增 `snake-hud.ts`，所以是 9 个。）

### 3.4 产物证据（`scripts/verify-m4b.sh` 的同款 grep + 第三个游戏）

```
packages/dsh-games/lib/client.js
  "ths-octop-games"                9 处
  sidebar.panellist                2 处
  五子棋                            3 处
  打飞机                            5 处
  贪吃蛇                            4 处
  win-board                        2 处
  MAX_STEPS_PER_TICK               3 处
  MAX_FRAME_MS                     2 处
  data-head                        1 处
  ths-octop-games-panel-snake      1 处

三个 panel id（去重）：
  ths-octop-games-panel-gomoku
  ths-octop-games-panel-plane
  ths-octop-games-panel-snake
```

引擎确实被内联进客户端产物（不是只留了个空壳）：`src/client/snake` 出现 3 处，
四句结束/胜利文案各 1 处（`撞到墙了` / `咬到自己了` / `蛇填满了整个盘面` / `达成分数目标`）。

host 产物 `packages/dsh-games/lib/index.js`：

```
games: ['gomoku', 'plane', 'snake']
```

### 3.5 仓库自带验收脚本（`bash scripts/verify-m4b.sh`，只读，未部署）

```
▸ 1/4 插件产物与 profile 装配
  ✓ dsh-radio 已部署（host + client）
  ✓ profile 已装配 @thsoctop/dsh-radio
  ✓ dsh-games 已部署（host + client）
  ✓ profile 已装配 @thsoctop/dsh-games
  ✓ 广播客户端含面板 id 与侧栏入口
  ✓ 小游戏客户端含两个游戏与侧栏入口
  ✓ 桌宠菜单含 open_watchlist / open_alerts / open_market / open_kline / open_radio / open_games / open_inspiration
  ✓ 客户端暴露面板钩子且声明了 layout 依赖
  ✓ 主窗口只在 URL 变化时导航
▸ 2/4 插件单测
  ✓ dsh-radio： Tests 8 passed (8)
  ✓ dsh-games： Tests 92 passed (92)      ← 本包全绿
  ✓ dsh-market： Tests 12 passed (12)
▸ 3/4 连接 host
  ✓ 复用桌面端 host（端口 57200）
▸ 4/4 广播清单与 HLS 同源代理
  ✓ 电台清单 200，播放地址全部是同源代理路径
  ✗ 播放列表代理异常（HTTP 504）  {"ok":false,"error":{"code":"TRANSPORT","message":"央广直播源暂时无法连接"}}
  ✓ 未知电台被拒（HTTP 404）
  ✓ 播报状态结构正确
  ✓ 手动播报真的送达桌宠（notify.show 回执 ok）
  ✓ 间隔被钳到 5 分钟下限
  ✓ 小游戏 host 接口 200（纯本地，无外部依赖）
────────────────────────────────────────
M4b 验收失败：25 项通过，1 项失败（唯一失败是电台 HLS 上游 504，与本任务无关）
```

### 3.6 改动范围

```
$ git status --short | grep -vc "dsh-games"
76          ← 基线 git-before.txt 里非 dsh-games 条目为 75
$ git status --short packages/dsh-games | wc -l
11
```

即：**本任务的全部源码改动落在 `packages/dsh-games` 内 11 个文件上，仓库其它地方一个源文件都没动。**

那 +1 是什么，如实说明：是本任务跑 `pnpm --filter … run test/typecheck/build` 时 pnpm 自动创建的
**本地缓存目录 `?? .pnpm-store/`（48K，内容是 `v11/index.db` 与 `files/` 内容寻址缓存，无任何源文件）**，
不在 `.gitignore` 覆盖范围内所以显示为未跟踪。它不是源码改动，也不是我手写的内容；
我没有删除它（避免破坏 pnpm 状态），也没有把它加进 `.gitignore`（那会动到包外文件）。
`git status --short packages/dsh-games` 之外没有任何源码文件被本任务修改。

另外，浏览器实测的临时 harness 与截图全部放在 `/tmp/snake-verify/`，**不在仓库内，不属于交付物**。

### 3.7 源码预览 vs 已部署运行时（如实区分）

| | 源码预览（本任务验证的） | 已部署运行时（未部署） |
| --- | --- | --- |
| 位置 | 仓库源码 + esbuild 产物 `packages/dsh-games/lib/` | `~/Library/Application Support/com.thsoctop.desktop/dependencies/dsh/node_modules/@thsoctop/dsh-games/` |
| 时间戳 | 09-30 11:55（本轮构建） | **09-28 18:29（早于本任务）** |
| `贪吃蛇` 命中 | 产物中 4 处 | **0 处** |
| host status | `games: ['gomoku','plane','snake']` | 实测 `games:["gomoku","plane"]` |

所以：**本轮所有验证都是针对「源码 + 新构建产物」的源码预览验证；本次任务不部署。**
真实 GUI 里要看到贪吃蛇 Tab，需要之后单独走一次部署（`scripts/bootstrap.sh --deploy-only` 之类），
本任务明确不做。监工独立在 `http://127.0.0.1:13082/` 用真实主题复核的那个第三个 Tab
是监工侧自行搭建的预览环境，同样不是本仓库的已部署运行时。

## 4. 已知限制（如实声明，不当作通过项）

1. **没有部署，也没有在仓库真实 GUI 里点过。** 已部署运行时（`~/Library/Application Support/com.thsoctop.desktop/dependencies/dsh`）
   的 `lib/` 时间戳是 09-28 18:29，早于本次改动；实测该 host 仍返回
   `games:["gomoku","plane"]`，其 `client.js` 里 `贪吃蛇` 命中 0 次。所以 **3.5 里那句
   「小游戏客户端含两个游戏」是部署产物仍为旧版的如实结果，不是新代码的缺陷**；
   要看到贪吃蛇 Tab 必须先在真实运行时里重新部署（本任务明确不做部署）。
   本轮改用**真实 Chrome + 真实 React + 真实 `SnakePanel`** 的临时 harness 做了等价验证（3.1.2），
   逐格移动/暂停/恢复/切 Tab 自动暂停/重开刷新食物/朝向即时响应都有 DOM 级证据；
   监工另在 `http://127.0.0.1:13082/` 用真实主题独立复核通过（源码预览，非本仓库已部署运行时）。
3. **交互路径没有仓库内的自动化回归测试。** harness 在 `/tmp` 且依赖人工触发，未纳入 `test/`；
   `test/` 里锁住的是引擎与「何时该重绘」的纯函数判据（`snake-hud.ts`），
   DOM 行为（焦点、IntersectionObserver、D-pad）目前只有一次性浏览器实测证据。
   harness 页面**未注入 `--dsw-alias-*` 主题 token**，所以截图里棋盘偏黑、只有硬编码色的食物可见；
   监工已用真实主题复核配色正确，**这是 harness 缺 token 的产物，不是代码缺陷，也未因此改配色**。
4. **最高分只在内存里**，刷新页面即丢；不落盘是本包既定约束（打飞机同样）。
5. 贪吃蛇**没有命数概念**，一次撞墙/咬到自己即结束；三档难度只调速度、盘面、穿墙与金苹果概率。
6. **满盘胜利在 17×17 上实战极难**，所以额外给了分数目标作为可达成的胜利条件；UI 同时显示进度。
   满盘胜利路径由单测（4×4 小盘）锁定，未做人工长局验证。
7. D-pad **不做长按连发**，快速连按两次完成一次 90° 转弯。
8. `verify-m4b.sh` 的 1 项失败是电台 HLS 上游 504（网络依赖），与本任务无关，未尝试修复。

## 5. 与既有两个游戏的关系

- 五子棋、打飞机的引擎与视图文件**未被修改**；只是 `index.tsx` 多了一个 Tab、`styles.ts` 追加了一段 CSS。
- `styles.ts` 的新增内容从第 99 行 `/* ---- 贪吃蛇 ---- */` 开始，既有规则（到第 97 行）一行未改；
  新选择器都带 `__snake-*` / `__pad*` 前缀或 `__stage--snake` 修饰类。
  **唯一影响既有元素的是末尾新增的 `@media (max-width: 720px)` 区块**，它会让整个小游戏面板在窄屏下
  内边距收窄为 `14px 14px 20px`、gap 收成 10px，并让 `.thsoctop-games__side`（三个游戏共用的侧栏）
  在小屏占满整行。这是有意的适配，但它对五子棋与打飞机同样生效。
