# 🍔 麦麦省钱助理

> 你的省钱盯价闹钟，到点叫你捡便宜。

基于**麦当劳官方 MCP Server** 的个人省钱 Agent Skill：盯住你常去门店的降价与券，
自动领券、算好到手价、备好下单卡；**该花钱的一步，永远等你一键确认**。

## 它替你做什么（自动边界两栏）

| ✅ 全自动（不用你管） | 🔒 需你一键确认（花钱动作） |
| --- | --- |
| 盯价巡检：常去门店菜单快照 + 跨期/跨店比价，检出「新低/降价」 | 下单 `create-order`（展示草稿 → 你确认 → 才执行） |
| 自动领券：一键领取麦麦省可领券（免费动作，留回执） | 积分兑换 `mall-create-order` |
| 算价：带券枚举，线性挑出最低到手价（积分券每单限 1 张、不叠加） | 取消订单 / 抽奖 |
| 临期积分提醒 + 常买商品 Top3 统计 | |
| 省钱战报卡：省 X 元 / 抓 Y 次降价，可截图分享（配方不含任何凭据） | |

## 快速开始（<2 分钟）

```bash
git clone <本仓库> && cd <本仓库>
cp config.example.json config.local.json
# 编辑 config.local.json：
#   1. mcp_token 填你的麦当劳 MCP Token（https://open.mcd.cn/mcp 手机号登录领取，免费）
#   2. user.default_address 填常用地址（查附近门店用）
python main.py warmup    # 首启热身：导入历史订单，建低价档案 + 常买 Top3
python main.py patrol    # 盯价巡检：降价 / 低价 / 可领券 / 临期积分
```

要求：Python 3.9+（WorkBuddy 内置解释器开箱即用），**零第三方依赖**，纯标准库。

## 常用命令

```bash
python main.py warmup                        # 冷启动：订单导入，建低价基线
python main.py patrol                        # 巡检：降价/新低/可领券/临期积分
python main.py stores --address "深圳市南山区科技园"   # 查附近门店
python main.py meals --store <storeCode> --save      # 看某店菜单优惠价（--save 刷新基线）
python main.py coupons                       # 一键领券（免费写，留回执）
python main.py best --store <code> --items "经典麦旋风 x1" [--track global|top3]
                                             # 带券枚举算最低价，产出订单草稿（只读）
python main.py order --draft <draft.json> --yes --confirm-text "确认下单"
                                             # ⚠️ 红线：必须你本人确认后才执行
python main.py share --draft <draft.json>    # 生成省钱战报卡 + 好友复现提示词
python main.py raw --tool query-my-account   # 调试：直接调任意只读工具
```

双轨推荐：`--track global`（全菜单价格最优）/ `--track top3`（你的购买量 Top3 里挑最优）。

## 配置说明（config.local.json）

> ⚠️ `config.local.json` 已被 `.gitignore` 排除，**永远不会提交**。模板见 `config.example.json`。

| 字段 | 说明 |
| --- | --- |
| `mcp_token` | 麦当劳 MCP Token（也可用环境变量 `MCD_MCP_TOKEN`，优先级更高）。领取：open.mcd.cn/mcp 手机号登录 → 控制台激活 |
| `user.nickname` | AI 怎么称呼你（默认「老大」，可改 老板/哥/靓仔） |
| `user.default_address` | 常用地址文本（MCP 不读 GPS，只吃地址） |
| `user.favorite_stores` | 常去门店 storeCode 列表（巡检范围） |
| `user.delivery_address` | 外卖配送地址（可选） |
| `agent.*` | 巡检门店数上限 / 新低阈值 / 临期积分阈值 |

本地数据（低价基线、菜单快照、商品档案）只存在 `~/.mcd-skill/`，**不收集、不上传**。

## WorkBuddy Agent Skill

本仓库自带 `skill.md`，在 WorkBuddy 中加载后即可对话式使用：
「盯一下麦当劳降价」「帮我领券」「麦旋风现在多少钱」「这单备好，确认后下」。

## 合规与声明

- 本项目为参赛作品，非麦当劳官方产品；仅供个人学习与非商业用途，
  使用即表示同意遵守麦当劳《使用条款》与《MCP 服务规则》。
- Token 仅保存在你本机 `config.local.json`，本工具不收集不上传任何个人数据。
- 价格记忆 = 本地观测到的「最低活动价」，非官方保证价；购买历史归纳为启发式建议。
- 限流：官方 600 次/分钟/token，客户端已内置滑动窗口自动控频。
