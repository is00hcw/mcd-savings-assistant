# -*- coding: utf-8 -*-
"""常用话术 → 命令映射（既是话术表，也是验收测试用例）

两个用途：
  ① 新用户照着念就能用（README「常用话术」章节由此生成）；
  ② Agent 拿到用户自然语句时有**确定映射**可查，而不是临场猜命令
     （skill.md 的映射表即由PHRASES 生成）。

红线：涉及花钱的 intent（confirm/order）永远只回显命令，**不自动执行**。
"""
from __future__ import annotations

import re

# intent → payload.type（Agent 据此选渲染形态）
PHRASES = [
    # ---------- 打招呼 / 元信息 ----------
    {"phrase": "你好，你会干嘛", "keys": ["你好", "会干嘛", "能干嘛", "介绍一下"],
     "intent": "capabilities", "cmd": "python main.py capabilities",
     "type": None, "note": "先讲清能做什么与红线，别急着要 Token"},
    {"phrase": "怎么用你 / 第一次用", "keys": ["怎么用", "第一次", "新手", "上手"],
     "intent": "doctor", "cmd": "python main.py doctor", "type": "alert",
     "note": "自检 8 项并给出下一步"},

    # ---------- 盯价 / 感知 ----------
    {"phrase": "麦当劳有什么优惠", "keys": ["有什么优惠", "有啥优惠", "优惠", "便宜"],
     "intent": "patrol", "cmd": "python main.py patrol", "type": "alert",
     "note": "最常用的一句话，等价于「帮我巡检一遍」"},
    {"phrase": "帮我盯一下降价", "keys": ["盯着", "盯一下", "盯价", "降价了", "降价"],
     "intent": "patrol", "cmd": "python main.py patrol", "type": "alert",
     "note": "盯价是持续任务，提醒用户可配定时巡检"},
    {"phrase": "附近有什么店 / 哪家店便宜",
     "keys": ["附近", "哪家店", "最近的店", "门店"],
     "intent": "stores", "cmd": "python main.py stores", "type": None,
     "note": "地址缺失会自动从麦乐送地址推导"},

    # ---------- 券 ----------
    {"phrase": "有券吗 / 能领什么券", "keys": ["有券", "什么券", "能领"],
     "intent": "coupons_preview", "cmd": "python main.py coupons", "type": "alert",
     "note": "默认只读预览，不动用户账户"},
    {"phrase": "帮我把券领了", "keys": ["领了", "领取", "领券"],
     "intent": "coupons_claim", "cmd": "python main.py coupons --claim --yes",
     "type": "alert", "need_confirm": True,
     "note": "免费动作但会写入账户，需用户明确说「领」才执行"},

    # ---------- 算价 / 选品 ----------
    {"phrase": "麦旋风现在多少钱", "keys": ["多少钱", "什么价", "价格", "多少钱啊"],
     "intent": "best", "cmd": 'python main.py best --store <门店code> --items "麦旋风 x1"',
     "type": "confirm", "need_confirm": True,
     "note": "先问清门店；算价只读，但产出的是待确认草稿"},
    {"phrase": "今天吃麦当劳哪个划算 / 最便宜",
     "keys": ["哪个划算", "最便宜", "划算", "怎么买便宜"],
     "intent": "best", "cmd": 'python main.py best --store <门店code> --items "<商品> x1"',
     "type": "confirm", "need_confirm": True,
     "note": "可加 --track top3 用其购买记录做个性化推荐"},
    {"phrase": "看看某店菜单", "keys": ["菜单", "有什么吃的", "看看菜单"],
     "intent": "meals", "cmd": "python main.py meals --store <门店code>",
     "type": None, "note": "顺便可加 --save 刷新低价基线"},

    # ---------- 下单确认闸 ----------
    {"phrase": "确认下单 / 就这个，下吧", "keys": ["确认下单", "就这个", "下单吧", "买吧"],
     "intent": "order", "cmd": 'python main.py order --draft <草稿.json> --yes --confirm-text "确认下单"',
     "type": "confirm", "need_confirm": True, "danger": True,
     "note": "唯一允许执行花钱动作的入口；草稿必须是刚展示给用户的那份"},
    {"phrase": "算了，先别买", "keys": ["算了", "先别", "不买", "取消"],
     "intent": "cancel", "cmd": "（不执行任何命令）", "type": None,
     "note": "礼貌收尾，不要再追问要不要下单"},

    # ---------- 复盘 / 分享 ----------
    {"phrase": "我一共省了多少钱", "keys": ["省了多少", "一共省", "省了多少钱"],
     "intent": "report", "cmd": "python main.py report --period month",
     "type": "report", "note": "默认按月，可换 today/week/all"},
    {"phrase": "发个战报 / 生成战报图", "keys": ["战报", "海报", "发个图", "分享图"],
     "intent": "share", "cmd": "python main.py share --draft <草稿.json>",
     "type": "share", "note": "图片用于对外转发，配方不含任何凭据"},

    # ---------- 配置 / 地址 ----------
    {"phrase": "帮我填好地址 / 我的地址在哪", "keys": ["地址", "填地址", "定位"],
     "intent": "address", "cmd": "python main.py address --apply",
     "type": "alert", "need_confirm": True,
     "note": "从麦乐送已存地址推导；写入前先给用户看推导结果"},
    {"phrase": "检查一下配置 / 能用吗", "keys": ["检查", "能用吗", "配置", "连不上"],
     "intent": "doctor", "cmd": "python main.py doctor", "type": "alert",
     "note": "会真实验证 Token 连通性"},
]

# 绝不自动执行白名单（与 mcd_client.MONEY_WRITE 呼应）
NEVER_AUTO_RUN = {"order", "coupons_claim", "address"}


def match(text: str) -> dict | None:
    """把一句用户话术映射到最近的意图。返回 PHRASES 中的一条 + 匹配到的关键词。"""
    if not text:
        return None
    t = str(text).strip().lower()
    best, best_hits, best_n = None, [], 0
    for item in PHRASES:
        hits = [k for k in item["keys"] if k.lower() in t]
        if len(hits) > best_n:
            best, best_hits, best_n = dict(item), hits, len(hits)
    if best:
        best["matchedKeys"] = best_hits
        return best
    return None


def runnable(intent: str) -> bool:
    """该意图是否允许自动执行（花钱动作一律False）。"""
    return intent not in NEVER_AUTO_RUN


def table_markdown() -> str:
    """生成 README / skill.md 用的话术表。"""
    groups = [
        ("打招呼与自检", ["capabilities", "doctor"]),
        ("盯价与选店", ["patrol", "stores", "meals"]),
        ("领券", ["coupons_preview", "coupons_claim"]),
        ("算价与下单", ["best", "order", "cancel"]),
        ("复盘与分享", ["report", "share"]),
        ("配置与地址", ["address"]),
    ]
    lines = ["| 你可以这样说 | 它会做什么 | 执行的命令 | 备注 |",
             "| --- | --- | --- | --- |"]
    for title, intents in groups:
        lines.append(f"| **{title}** | | | |")
        for it in PHRASES:
            if it["intent"] not in intents:
                continue
            desc = it["note"]
            if it.get("danger"):
                desc = "⚠️ " + desc
            elif it.get("need_confirm"):
                desc = "需确认：" + desc
            lines.append(f'| 「{it["phrase"]}」 | {it["intent"]} | `{it["cmd"]}` | {desc} |')
    return "\n".join(lines)


def cases() -> list[dict]:
    """验收测试用例：话术 → 期望意图 + 期望 payload 类型。"""
    return [{"say": p["phrase"], "expect_intent": p["intent"], "expect_type": p["type"],
             "auto_runnable": runnable(p["intent"])} for p in PHRASES]