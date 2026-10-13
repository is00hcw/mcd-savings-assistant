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
| 省钱账单与战报：省 X 元 / 抓 Y 次降价，可截图分享（配方不含任何凭据） | |

## 输出形态（三层，同数据源多渲染）

核心逻辑只产出**结构化 payload**（带 `type` 字段），渲染层可插拔：

| type | 场景 | 形态 | 命令 |
| --- | --- | --- | --- |
| `alert` | 降价提醒 / 领券回执 / 到点提醒 / 积分临期 | 内联卡片（对话流内直接看） | `patrol` / `coupons` |
| `confirm` | 下单确认闸 / 双轨候选二选一 | 交互卡片（确认/取消按钮） | `best` |
| `report` | 省钱账单（细看/存档） | HTML 页面 | `report` |
| `share` | 省钱战报（对外转发） | 图片 SVG | `share` |

每个命令都支持 `--format inline|json|html|svg`，例如：

```bash
python main.py patrol  --format inline           # 内联卡片（默认，任何宿主可渲染）
python main.py best    --format json             # 拿结构化 payload 自己渲染
python main.py report  --format html --out bill.html
python main.py share   --format svg  --out war.svg
python main.py render  --payload pay.json --format html   # 渲染层可插拔验证
```

设计原则：**图片只用于分享、不用于交互**——确认闸永远是卡片/按钮，绝不靠图片。

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
python main.py best --store <code> --items "经典麦旋风 x1" [--track global|top3]
                                             # 带券枚举算最低价，产出 confirm 确认卡（只读）
python main.py order --draft <draft.json> --yes --confirm-text "确认下单"
                                             # ⚠️ 红线：必须你本人确认后才执行
python main.py report --period month          # 省钱账单 → HTML 页面
python main.py share --draft <draft.json>     # 省钱战报 → 图片 + 好友复现提示词
python main.py raw --tool query-my-account   # 调试：直接调任意只读工具
```

双轨推荐：`--track global`（全菜单价格最优）/ `--track top3`（你的购买量 Top3 里挑最优）。

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
