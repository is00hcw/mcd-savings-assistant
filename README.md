# 🍔 麦麦省钱助理

> 你的省钱盯价闹钟，到点叫你捡便宜。

基于**麦当劳官方 MCP Server** 的个人省钱 Agent Skill：盯住你常去门店的降价与券，
自动领券、算好到手价、备好下单卡；**该花钱的一步，永远等你一键确认**。

**项目介绍**：麦当劳 MCP 把「菜单优惠价 / 卡包券 / 订单历史 / 积分」这些原本只能手动翻的
数据摊开了。这个 Skill 把它们串成一条闭环——**感知（盯价巡检）→ 决策（带券枚举算最低到手价）
→ 执行（生成下单卡）→ 复盘（省钱账单与战报）**，让「今天哪顿最划算」变成被动收到的提醒，
而不是你每天自己去比价。产品定位是副驾而非自动扣款工具：所有花钱动作都必须经过用户本人确认。

## 目标用户

- **日常高频买麦当劳的上班族/学生**：不想每天刷菜单比价，只想被告知「这家今天便宜了」。
- **有积分/券屯着不用的人**：临期积分提醒 + 一键领券，避免过期作废。
- **价格敏感型家庭/合租党**：多家门店跨店比价，锁定最便宜的那一家。
- **想学 Agent Skill 改造自己日常服务的开发者**：零第三方依赖、模块边界清晰、读写白名单与
  人类确认闸是可直接复用的安全范式。
- 不适合：需要全自动下单的无人值守场景（本项目刻意不做自动扣款）。

## 它替你做什么（自动边界两栏）

| ✅ 全自动（不用你管） | 🔒 需你一键确认（花钱动作） |
| --- | --- |
| 盯价巡检：常去门店菜单快照 + 跨期/跨店比价，检出「新低/降价」 | 下单 `create-order`（展示草稿 → 你确认 → 才执行） |
| 可领券预览（默认只读，不动账户） | 积分兑换 `mall-create-order` |
| 领券：显式 `--claim --yes` 才写账户，免费动作留回执 | 取消订单 / 抽奖 |
| 算价：带券枚举，线性挑出最低到手价（积分券每单限 1 张、不叠加） | |
| 临期积分提醒 + 常买商品 Top3 统计 | |
| 省钱账单与战报：实际省下 X 元（真实下单）+ 潜在优惠 Y 元（盯价估算，分开展示）+ 抓 Z 次降价，可截图分享（配方不含任何凭据） | |

## 输出形态（三层，同数据源多渲染）

核心逻辑只产出**结构化 payload**（带 `type` 字段），渲染层可插拔：

| type | 场景 | 形态 | 命令 |
| --- | --- | --- | --- |
| `alert` | 降价提醒 / 领券回执 / 到点提醒 / 积分临期 | 内联卡片（对话流内直接看） | `patrol` / `coupons` |
| `confirm` | 下单确认闸 / 双轨候选二选一 | 交互卡片（确认/取消按钮） | `best` |
| `report` | 省钱账单（细看/存档） | HTML 页面 | `report` |
| `share` | 省钱战报（对外转发） | 图片 SVG | `share` |

**产出 payload 的命令**支持 `--format inline|json|html|svg`（`patrol` / `best` / `coupons` /
`report` / `share` / `doctor` / `capabilities` / `render`），例如：

```bash
python main.py patrol  --format inline           # 内联卡片（默认，任何宿主可渲染）
python main.py best    --format json             # 拿结构化 payload 自己渲染
python main.py report  --format html --out bill.html
python main.py share   --format svg  --out war.svg
python main.py render  --payload pay.json --format html   # 渲染层可插拔验证
```

`best --format json` 的 stdout 固定为 JSON envelope，是否提供 `--out` 都保持相同结构：
`payload` 是确认卡数据，`saved` 表示是否另存 payload，`payloadPath` 未保存时为 `null`，
`draftPath` 指向待确认草稿，双轨模式还会提供 `top3DraftPath`（单轨时为 `null`）。

过程型命令（`schedule` / `order` / `meals` / `stores` / `warmup` / `raw`）不产 payload，
输出文本进度或原始 JSON，**没有** `--format` 参数。

设计原则：**图片只用于分享、不用于交互**——确认闸永远是卡片/按钮，绝不靠图片。

> **关于「交互卡片」**：payload 的 `actions[]` 携带 `{id, label, command}` 元数据，
> 其中 `command` 是给 Agent/宿主执行的指令（含 `--yes` 等凭据，不展示给终端用户）。
> **普通 Markdown 的 `[确认下单]` 文本本身不可点击**——真正的可点按钮需要宿主
> （如 WorkBuddy）识别 `actions[]` 后单独接线；纯 CLI 下你看到的是文字，需手动回复
> 「确认下单」并执行 `order --draft ...` 命令。

### 消息格式规范（四种场景的样例）

**① 到点确认消息**（`type=confirm`，最常用，信息最全）：

```
🧾 到点了：加班餐
老大，18:30 的单我按此刻的价格重新算好了：原价 44.5，到手 39.5，省 5，用券「9.9元中杯冰美式」（有效期至 2026-10-18 23:59:59）。你确认我就下，不确认我不动。
- **18:30**　到点时间
- 44.5元　原价
- **39.5元**　到手价
- **5.0元**　省
- 18:30　报价时间

- 薯条 × 2　¥29（中薯条 · 单价 ¥14.5）
- 经典麦旋风 × 1　¥15.5（单价 ¥15.5）

[确认下单]　[再想想]

> 到点任务 sch-20261013-121956｜加班餐｜麦当劳深圳金环宇大厦餐厅（堂食）｜券：9.9元中杯冰美式（有效期至 2026-10-18 23:59:59）｜⛔ 这条只是确认消息，我不会自动下单。｜报价 18:30，30 分钟内有效，超时需重新确认
```

要素：**到点时间 · 报价时间 · 原价/到手价/省 · 逐件商品（数量·小计·官方规格名·单价）· 门店与取餐方式 · 用的哪张券+券到期时间 · 报价有效期 · 明确的下一步 · 红线声明**。

**② 降价提醒**（`type=alert`）：

```
🔔 降价提醒
老大！蘸酱炸鸡 从 14.5 块降到 11.9 块，刷新历史最低！（门店 1421252）要不要我把单备好？
- **2.6元**　按历史最低省
- **1个**　抓到降价
- 2家　巡检门店

- 蘸酱炸鸡　¥11.9　（历史最低 14.5）
```

**③ 领券回执**（`type=alert`）：领到几张、卡包现有几张、哪几张券可领。

**④ 到点吃饭提醒**（`type=alert`，无商品登记时）：列出附近可点的门店；登记了商品则直接列出那一单，并注明「需你确认」。

> 同一份数据可渲染为内联卡片（上面这些）、HTML 页面（`--format html`）、
> 图片（`--format svg`）。**图片只用于分享，确认闸永远用卡片/按钮。**

## 定时下单 / 到点提醒（到点只推确认消息，绝不自动下单）

> 定位说明：定时任务产出的是**即时单**——`--at` 只是「触发重算价 + 提醒」的时间，
> 到点推送确认卡，你确认后按**当下**价格下单、当下取餐。**不做预约单**
> （草稿的 `reservationDate` 恒为空，仅兼容预留）；要预约未来取餐请到 App 操作。

```bash
# 登记：18:30 在金环宇店买「麦旋风 x1」，自动用最优券
python main.py schedule add --at "18:30" --store 1421252 --items "麦旋风 x1" --note "加班餐"

# 到点了 / 让定时任务替你问一遍（到点时刻该命令由系统计划任务或 WorkBuddy 定时任务调用）
python main.py schedule due --fire

# 其他管理
python main.py schedule list            # 查看全部（--pending 只看未触发）
python main.py schedule cancel <id>     # 取消（保留记录）
python main.py schedule rm <id>         # 删除
python main.py schedule watch           # 常驻轮询（本地调试用，生产建议交给定时任务）
```

**到点会发生什么**（严格三步，绝不越过）：

1. **重新算价**——用**此刻**的菜单价与券重新算（券可能过期、特价可能已结束）；
2. **推送确认消息**——生成 confirm 确认卡（原价/到手价/省多少/用的哪张券），
   落盘到 `~/.mcd-skill/outbox/`，并在消息里附上确认后才执行的命令；
3. **停下等你**——你回「确认下单」后才走 `order --yes` 真正下单。

```
⏰ 到点任务 1 条
--- 推送内容 ---
🧾 到点了：加班餐
老大，18:30 的单我算好了：原价 16.5，到手 14.5，省 2.0。（价格与券已按此刻重新计算）
你确认我就下，不确认我不动。
⛔ 这条只是确认消息，我不会自动下单。
```

**三重保险**（不是靠自觉，是靠机制）：

| 防线 | 实现 |
| --- | --- |
| 客户端白名单 | `create-order` 等花钱工具无 `confirm=True` 直接拒绝 |
| 到点客户端外壳 | `_NoMoneyClient` **默认拒绝**：只放行显白名单的只读方法，调用花钱接口/方法一律 `PermissionError`（连 `auto-bind-coupons` 也不许） |
| 草稿确认闸 | 到点产出的草稿强制 `confirmedByHuman=false`，无用户确认执行必被拒 |
| 确认重算价 | 下单前按草稿重新算价并比对，价格/券/商品变了就拒绝；草稿报价超 30 分钟或券已过期也拒绝；`--confirm-text` 必须精确等于「确认下单」 |
| 写操作禁重试 | 所有写操作（下单/取消/领券/建地址）遇网络错误**不自动重试**，避免「服务端已建单但响应丢失」时重复下单；结果不确定时改查订单状态再提示用户到 App 核对 |

也就是说：**即使代码将来出 bug，到点也不可能自己下单。** 而且下单那一刻若价格/券已变，同样会被拦下要求重新确认。

配合系统定时任务（Windows 任务计划 / cron）：

```bash
*/10 * * * * cd /path/to/mcd-savings-assistant && python main.py schedule due --fire
```

在 WorkBuddy 里也可以让我建一个定时任务，每 10 分钟跑一次 `schedule due --fire`，
到点有单子我就把确认卡推给你。

## 常用话术（照着说就行 · 同时也是验收用例）

在 WorkBuddy 里直接说人话即可；纯 CLI 玩法可以先查映射：
`python main.py phrase "麦当劳有什么优惠"`。

| 你可以这样说 | 它会做什么 | 执行的命令 | 备注 |
| --- | --- | --- | --- |
| **打招呼与自检** | | | |
| 「你好，你会干嘛」 | capabilities | `python main.py capabilities` | 先讲清能做什么与红线，别急着要 Token |
| 「怎么用你 / 第一次用」 | doctor | `python main.py doctor` | 自检 8 项并给出下一步 |
| 「检查一下配置 / 能用吗」 | doctor | `python main.py doctor` | 会真实验证 Token 连通性 |
| **盯价与选店** | | | |
| 「麦当劳有什么优惠」 | patrol | `python main.py patrol` | 最常用的一句话，等价于「帮我巡检一遍」 |
| 「帮我盯一下降价」 | patrol | `python main.py patrol` | 盯价是持续任务，提醒用户可配定时巡检 |
| 「附近有什么店 / 哪家店便宜」 | stores | `python main.py stores` | 地址缺失会自动从麦乐送地址推导 |
| 「看看某店菜单」 | meals | `python main.py meals --store <门店code>` | 顺便可加 `--save` 刷新低价基线 |
| 「我想点外卖 / 麦乐送送到家」 | delivery | `python main.py delivery` | 先查配送地址，再 `--address-id` 查可配送门店 |
| **领券** | | | |
| 「有券吗 / 能领什么券」 | coupons_preview | `python main.py coupons` | 默认只读预览，不动用户账户 |
| 「帮我把券领了」 | coupons_claim | `python main.py coupons --claim --yes` | 需确认：免费动作但会写入账户，需用户明确说「领」才执行 |
| **算价与下单** | | | |
| 「麦旋风现在多少钱」 | best | `python main.py best --store <门店code> --items "麦旋风 x1"` | 需确认：先问清门店；算价只读，但产出的是待确认草稿 |
| 「今天吃麦当劳哪个划算 / 最便宜」 | best | `python main.py best --store <门店code> --items "<商品> x1"` | 需确认：可加 `--track top3` 用其购买记录做个性化推荐 |
| 「算一下外卖多少钱 / 送到家多少钱」 | best_delivery | `python main.py best --delivery --address-id <地址id> --items "<商品> x1"` | 需确认：麦乐送外卖算价（含配送费/打包费），门店可省略自动选可配送门店 |
| 「确认下单 / 就这个，下吧」 | order | `python main.py order --draft <草稿.json> --yes --confirm-text "确认下单"` | ⚠️ 唯一允许执行花钱动作的入口；草稿必须是刚展示给用户的那份 |
| 「算了，先别买」 | cancel | （不执行任何命令） | 礼貌收尾，不要再追问要不要下单 |
| 「取消所有未支付订单」 | cancel_unpaid | `python main.py cancel`（预览）→ `python main.py cancel --yes --confirm-text "确认取消"` | ⚠️ 取消不可逆；仅覆盖近期可见订单，更早的未支付单需到 App 处理 |
| **复盘与分享** | | | |
| 「我一共省了多少钱」 | report | `python main.py report --period month` | 默认按月，可换 today/week/all |
| 「发个战报 / 生成战报图」 | share | `python main.py share --draft <草稿.json>` | 图片用于对外转发，配方不含任何凭据 |
| **配置与地址** | | | |
| 「帮我填好地址 / 我的地址在哪」 | address | `python main.py address --apply` | 需确认：从麦乐送已存地址推导；写入前先给用户看推导结果 |

想看机器可读的验收用例：`python main.py phrase`（话术表）、
`python -c "from src.phrases import cases; print(cases())"`（结构化用例）。

## 首次使用（3 步，2 分钟）

```bash
git clone https://github.com/is00hcw/mcd-savings-assistant.git
cd mcd-savings-assistant

# ① 生成配置模板（也可以直接 cp config.example.json config.local.json）
python main.py init

# ② 填 Token：手机号登录 https://open.mcd.cn/mcp → 右上角【控制台】→【激活】→ 复制
#    粘贴到 config.local.json 的 mcp_token 字段（仅本机，已被 .gitignore 排除）
#    也可以改用环境变量：export MCD_MCP_TOKEN=xxx（优先级更高）

# ③ 自检 → 热身 → 开始盯价
python main.py doctor        # 全绿即配置完成（会实际验证 Token 连通性）
python main.py warmup        # 导入历史订单，建低价档案与常买 Top3
python main.py patrol        # 巡检常去门店：降价 / 可领券 / 临期积分
```

**忘了 Token 放哪、或不确定配好了没？** 随时跑 `python main.py doctor`：
它会逐项检查 Python 版本、配置文件、Token 是否有效（真实调官方接口验证）、
常用地址与常去门店是否就绪，并直接给出下一步该做什么。

**不确定这个 Skill 能干什么？** 跑 `python main.py capabilities`，
它会打印完整能力清单、输出契约和红线（也给 AI 助手读）。

| 我想… | 跑这条 |
| --- | --- |
| 让它盯降价 | `python main.py patrol` |
| 算某样东西今天最便宜多少 | `python main.py best --store <门店code> --items "麦旋风 x1"` |
| 看这门技能有没有坑 | `python main.py doctor` |
| 自动填常用地址 | `python main.py address --apply` |
| 看我一共省了多少 | `python main.py report --period month` |
| 生成能发朋友圈的战报 | `python main.py share --draft <草稿.json>` |

> 直接在 WorkBuddy 里对话使用也可以，`skill.md` 已写好全部指令（见下方「WorkBuddy Agent Skill」）。

## 快速开始（等价于上面首次使用）

```bash
git clone <本仓库> && cd <本仓库>
cp config.example.json config.local.json
# 编辑 config.local.json：
#   1. mcp_token 填你的麦当劳 MCP Token（https://open.mcd.cn/mcp 手机号登录领取，免费）
#   2. user.default_address 填常用地址（查附近门店用）
python main.py warmup    # 首启热身：导入历史订单，建低价档案 + 常买 Top3
python main.py patrol    # 盯价巡检：降价 / 低价 / 可领券 / 临期积分（alert 卡片）
```

要求：Python 3.9+（WorkBuddy 内置解释器开箱即用），**零第三方依赖**，纯标准库。

## 常用命令

```bash
python main.py warmup                        # 冷启动：订单导入，建低价基线
python main.py patrol                        # 巡检：降价/新低/可领券/临期积分 → alert 卡
python main.py stores --address "深圳市南山区科技园"   # 查附近门店
python main.py meals --store <storeCode> --save      # 看某店菜单优惠价（--save 刷新基线）
python main.py coupons                       # 可领券预览（只读，不动账户）
python main.py coupons --claim --yes         # 一键领券（免费写，留回执）
python main.py delivery                      # 查配送地址 + 可配送门店（外卖前置，只读）
python main.py best --store <code> --items "经典麦旋风 x1" [--track global|top3|dual]
                                             # 到店自提：带券枚举算最低价，产出 confirm 确认卡（只读）
python main.py best --delivery --address-id <id> --items "麦乐鸡5块 x1" [--store <code>]
                                             # 麦乐送外卖：自动选可配送门店，价含配送费/打包费
python main.py order --draft <draft.json> --yes --confirm-text "确认下单"
                                             # ⚠️ 红线：必须你本人确认后才执行
python main.py cancel                         # 预览「近期可见」的未支付订单（只读，不取消）
python main.py cancel --yes --confirm-text "确认取消"
                                             # ⚠️ 红线：逐单取消未支付订单，须本人确认
python main.py report --period month          # 省钱账单 → HTML 页面
python main.py share --draft <draft.json>     # 省钱战报 → 图片 + 好友复现提示词
python main.py raw --tool query-my-account   # 调试：直接调任意只读工具
```

轨道选择：`--track global`（全菜单价格最优，默认）/ `--track top3`（你的购买量
Top3 里挑最优）单轨直出；`--track dual` 双轨候选二选一——两条同时算价，
出「双轨候选」确认卡，确认哪条就执行哪条草稿的 order 命令。

定时任务 `--coupon`：`auto`（默认）到点重算最优券；也可填具体券 id/code，
到点只测「无券基线 + 该券」，该券不可用时确认卡会注明其生效时间窗并按无券价计。

## 配置说明（config.local.json）

> ⚠️ `config.local.json` 已被 `.gitignore` 排除，**永远不会提交**。模板见 `config.example.json`。

| 字段 | 说明 |
| --- | --- |
| `mcp_token` | 麦当劳 MCP Token（也可用环境变量 `MCD_MCP_TOKEN`，优先级更高）。领取：open.mcd.cn/mcp 手机号登录 → 控制台激活 |
| `user.nickname` | AI 怎么称呼你（默认「老大」，可改 老板/哥/靓仔） |
| `user.default_address` | 常用地址文本（MCP 不读 GPS，只吃地址）。**可自动推导**，见下节 |
| `user.favorite_stores` | 常去门店 storeCode 列表（巡检范围），也可从订单推导 |
| `user.delivery_address` | 外卖配送地址（可选） |
| `agent.*` | 巡检门店数上限 / 新低阈值 / 临期积分阈值 |

本地数据（低价基线、菜单快照、商品档案、省钱账本）只存在 `~/.mcd-skill/`，**不收集、不上传**。

## 个人信息不用手填：自动推导

```bash
python main.py address                 # 只读预览推导结果（不写盘）
python main.py address --apply         # 认可就回填到 config.local.json（本地文件，不进 git）
python main.py warmup --apply-address --apply-stores   # 首启一并回填地址 + 常去门店
```

推导优先级：

1. **`delivery-query-addresses`（权威）**——你自己在麦乐送保存的地址，直接可喂给
   `query-nearby-stores`；多条时选与常去门店**同片**的那条（比对地址/门店名 token）。
2. **订单 `storeAddress`（兜底）**——订单只能推**门店**地址，推不出你家门牌，
   但可作为「查周边门店」的锚点。
3. 都拿不到才需要你手填。

隐私红线：手机号与联系人**一律不落盘**（接口本身返回的也是掩码号）；回写函数有字段白名单，
`mcp_token` 之类敏感字段写不进去；地址只进 gitignore 的本地文件。

`stores` 命令在地址缺失时会自动用推导结果查店（并打印地址来源），无需先手工配置。

## WorkBuddy Agent Skill

本仓库自带 `skill.md`，在 WorkBuddy 中加载后即可对话式使用：
「盯一下麦当劳降价」「帮我领券」「麦旋风现在多少钱」「这单备好，确认后下」。

## 合规与声明

- 本项目为参赛作品，非麦当劳官方产品；仅供个人学习与非商业用途，
  使用即表示同意遵守麦当劳《使用条款》与《MCP 服务规则》。
- Token 仅保存在你本机 `config.local.json`，本工具不收集不上传任何个人数据。
- 价格记忆 = 本地观测到的「最低活动价」，非官方保证价；购买历史归纳为启发式建议。
- 限流：官方 600 次/分钟/token，客户端已内置滑动窗口自动控频。
