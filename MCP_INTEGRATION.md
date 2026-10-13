# MCP 集成说明

## 接入方式

- 端点：`https://mcp.mcd.cn`
- 协议：Streamable HTTP，JSON-RPC 2.0
- 鉴权：`Authorization: Bearer <token>`（token 存本机 `config.local.json` 或环境变量 `MCD_MCP_TOKEN`）
- 限流：600 次/分钟/token（超限 429，客户端内置滑动窗口自动控频并留 5 次余量）

调用序列：`initialize` → `notifications/initialized` → `tools/list` → `tools/call`。
响应兼容：直接 JSON 或 SSE（`data:` 行取最后一条）；业务错误信封
（`success:false, code, message`）会被客户端转成异常。

## 工具使用清单

| 工具 | 用途 | 模块 | 读/写 |
| --- | --- | --- | --- |
| `query-meals` | 菜单优惠价（currentPrice/originalPrice，**单位=元**，已实测）；`data.meals` 为 dict 且 key=code，value 可能不含 code 字段 | A/B 低价记忆、比价 | 只读 |
| `query-meal-detail` | 套餐组成 | D 选品 | 只读 |
| `query-nearby-stores` | 地址文本查附近门店（含预约时段） | D 定店 | 只读 |
| `query-my-coupons` | 卡包券（实测 `data.coupons[]`，名称字段=`title`） | A/C/D 用券 | 只读 |
| `query-store-coupons` / `available-coupons` | 门店券 / 可领券（`couponName`/`couponStatus`） | B/C | 只读 |
| `auto-bind-coupons` | 一键领取可领券 | C 自动领券 | 写·免费（自动，留回执） |
| `calculate-price` | 带券算价（**单位=分**，已实测）。必带 `beType`(1-到店/2-外送/5-得来速/6-团餐)、`orderType`(1-到店/2-外送)、`beCode`("1")；返回 `takeWayList[].code` 如 `eat-in`/`locker-in`；券挂载在 item 上：`couponId`+`couponCode` | D 最优下单 | 只读 |
| `create-order` | 创建订单（支持 `reservationDate` 预约） | G 下单 | **写·花钱（必须人类确认）** |
| `cancel-order` | 取消订单 | 兜底 | **写·花钱（必须人类确认）** |
| `order-list` | 近 10 笔订单（无单品价格） | A 冷启动 | 只读 |
| `query-order` | 订单详情（`totalDiscountAmount` 判定整单有优惠） | A 冷启动佐证 | 只读 |
| `query-my-account` | 积分（`currentMouthExpirePoint`=本月将过期） | E 提醒 | 只读 |
| `campaign-calendar` / `now-time-info` | 月度活动 / 当前时间 | B/G | 只读 |
| `mall-points-products` / `mall-product-detail` / `mall-create-order` | 积分商城（v2） | E | 兑换下单须确认 |

## 核心调用流程

```
【首启热身】order-list → query-order(逐单) → 本地建低价基线 + 常买 Top3
【盯价巡检】query-my-account(临期积分) + available-coupons(可领券) + campaign-calendar
          → 逐店 query-meals → 快照对比(跨期降价) + 基线对比(历史新低) + 跨店互比
【最优下单】query-nearby-stores(定店) → query-meals(选品, name 归一化→取 code)
          → query-my-coupons + query-store-coupons(券候选)
          → calculate-price(无券基线 + 每张券线性各算一次) → 取最低 → OrderDraft
          → 【人类确认】→ create-order(takeWayCode 来自 takeWayList)
【领券】  available-coupons → auto-bind-coupons → query-my-coupons(校验入库)
```

## 实测踩坑记录（2026-10-13，真实 token）

1. `query-meals` 价格单位=**元**；`calculate-price` 单位=**分**。两套单位并存。
2. `data.meals` 是 dict（key=code），value 内 `code` 可能为 null，需从 key 注入。
3. `calculate-price` 不带 `beType/orderType/beCode` 直接 400，报错文案里给出了枚举。
4. 券挂载在 `items[0]`（`couponId`+`couponCode`）；不适用时返回业务错误
   `600012 促销规则不支持该商品` / `当前优惠券不可用`，枚举策略按跳过处理。
5. 券对象含 `beTypes/orderTypes/weekdays/tradeTimePeriods` 等约束字段，
   但**未暴露适用商品 code 清单**——「券×商品」匹配只能靠算价试探。
6. 同商品跨店 code 不同（如「吉士蛋麦满分组合」两店 code 不同），归一化主键必须用 name。

## 安全边界

- 读写白名单硬编码在 `src/mcd_client.py`：`MONEY_WRITE` 集合的工具在客户端层
  拒绝无 `confirm=True` 的调用；CLI 层再叠加草稿确认闸（`confirmedByHuman` 等三标记）。
- 分享卡的 order recipe 只含门店/商品/券码，**不含 token 与个人凭据**。
