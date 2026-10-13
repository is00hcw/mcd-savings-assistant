# workbuddy.md · 开发过程存档

## 项目

「麦麦省钱助理」——基于麦当劳官方 MCP Server 的 WorkBuddy Agent Skill。
定位：省钱盯价闹钟（副驾）。MVP = 一条闭环（盯价→领券→算价→确认下单）+ 一张卡（省钱战报）。

## 时间线（2026-10-13，一天完成 MVP）

### 上午 · 脚手架与安全边界

- 按《产品方案 v0.14》第 12 节规格建仓：`src/` 九模块 + `main.py` CLI + `tests/`。
- **Token 隔离设计**：真实 token 只写 `config.local.json`（进 `.gitignore`）；
  环境变量 `MCD_MCP_TOKEN` 优先级更高；仓库内只有 `config.example.json` 占位模板。
  个人信息（地址/称呼/收藏门店）同落该文件。
- 客户端三道闸：`MONEY_WRITE` 白名单（客户端层拒无 confirm 调用）
  → 草稿确认闸（`confirmedByHuman/confirmedAt/confirmedText` 三标记）
  → CLI `--yes --confirm-text` 显式凭据。

### 上午 · 实测定案（真实 token 只读）

- `now-time-info` / `query-my-account` 首通：JSON-RPC + structuredContent 链路 OK，
  账户 841.1 分、本月将过期 114.6 分。
- **价格单位实锤**：`query-meals`=元、`calculate-price`=分（方案文档「需核对」项①关闭）。
- `data.meals` 是 dict（key=code），value 内 code 为 null → 客户端注入。
- `calculate-price` 必带 `beType/orderType/beCode`（400 报错文案给出枚举）；
  `takeWayList[].code` 实测为 `eat-in`/`locker-in`。
- 券结构实锤：卡包 `data.coupons[]`（名称=`title`），可领券 `couponName/couponStatus`。
- 券挂载 items[0]（couponId+couponCode）参数正确；「促销规则不支持该商品/当前优惠券不可用」
  为券规则细节，官方未暴露适用商品清单 → 枚举按跳过处理（已知边界）。
- 「人气麦旋风买一送一」券 `beTypes:[4]` 与 calculate-price 枚举(1/2/5/6)不一致，
  beType=4 报门店配对失败 → 疑似券侧标签口径不同，记录待深挖。

### 下午 · 全链路联调

- `warmup`：订单导入 14 件、8 件"买过优惠"佐证、Top3 = 麦旋风任选/人气经典随心配/小食二选一。
- `patrol`：双店巡检 → 跨店新低检出正常；两轮降噪修复
  （① 无折扣品不报"新低"；② 历史低价榜只收现价<原价的商品）。
- `best`：¥15.5 麦旋风算价正确；草稿落盘 `~/.mcd-skill/drafts/`。
- `order`：无 `--yes` 红线拒绝路径验证通过（exit=4，展示草稿不执行）。
- `share`：战报卡 HTML 生成，二维码走自研零依赖 QR（字节模式，L 级，v1–5，106B 上限），
  超限自动降级纯文本配方。

### 踩坑修复

1. Python 3.13 `from x import a, b,  # 注释` 续行语法不允许 → 改括号包裹。
2. 业务的错误信封（success:false）原样透传导致静默 0 元 → 客户端层统一转异常。
3. f-string `{:>8:g}` 非法 → `{:>8.2f}`。

## v0.15 增量：14.3 输出展示形态规范（同数据源多渲染）

方案升级到 v0.15 后新增 14.3，原实现是「直接打印文本 + 生成 HTML」，缺少统一结构化层。
本轮把输出层重做为可插拔架构：

- **新增 `src/present.py`**（渲染层）：payload schema + 四渲染器
  （inline 卡片 / html 页面 / svg 图片 / json 兜底），`render(payload, fmt)` 单一入口。
- **新增 `src/payloads.py`**（映射层）：业务对象 → payload。
  alert: 降价提醒 / 领券回执 / 积分临期 / 可领券；confirm: 下单确认闸 + 双轨候选；
  report: 省钱账单；share: 战报（带 order recipe）。
- **新增 `src/ledger.py`**（账本）：`~/.mcd-skill/ledger.jsonl`，
  drop=盯价估算省额、order=真实优惠，为 H 模块提供可聚合数据源。
- **CLI 全量改造**：所有命令统一 `--format inline|json|html|svg`；
  新增 `report`、`render` 命令；`coupons` 改为**默认只读预览**，
  需 `--claim --yes` 才写账户（比原方案更保守，也更符合 15.4 安全边界）。

四条原则落到代码：

| 原则 | 落地 |
| --- | --- |
| ① 同数据源多渲染 | payload 里只放数据 + type，不含任何 HTML 片段 |
| ② 组件优先于 HTML | alert/confirm 默认 inline，不生成文件 |
| ③ 跨宿主降级 | inline 即 Markdown，纯 CLI 也能完整显示 |
| ④ 图片不用于交互 | svg 只含展示信息；确认动作只出现在 actions[] |

踩坑修复（4 处）：

1. `render_svg` 首版残留废弃的两段式布局逻辑（全局变量 H_BODY_H），重写为「先量高度再输出」单遍。
2. SVG 首个指标与摘要行重叠（y=96 vs y=90）→ 指标起点下移至 y=128，目视校验通过。
3. 确认卡里商品名显示成 code → `optimize.build_items` 回填 name（calculate-price 忽略多余字段）。
4. `coupons`/`patrol` 把「已领取」的券也算进可领数 → 按 label 过滤 HAVING_RECEIVE。

实测（只读，未下单/未领券）：patrol → 3 张 alert 卡；best → confirm 卡带 [确认下单]/[再想想]；
report → HTML 账单；share → SVG 战报（二维码可扫，目视校验版式）；
`order` 无 `--yes` 仍按红线拒绝（exit 4）。离线测试从 5 组扩到 7 组，新增
present 四渲染 + confirm 动作完整性 + ledger 聚合。

## 设计取舍

- **零第三方依赖**：QR 码自研（v1–5 单块）换取 clone 即用；配方 JSON 压缩到 106 字节内。
- **券枚举线性化**：积分券「一单限一张不叠加」（实测券面须知），组合爆炸退化为 N 次算价。
- **归一化主键=name**：同商品跨店 code 不同（实测两店吉士蛋麦满分组合异码）。
- **巡检降噪**：无折扣常价商品不进事件流，避免"狼来了"。

## 待办（v2）

- 券×商品匹配规则深挖（promotionId 关联查询？）
- 定时巡检接 WorkBuddy 自动化（automation）
- E 积分兑换 / F 个性化推荐（数据源已探明）
- 二维码扩容（v6+ 多块交织）或改用短链配方
