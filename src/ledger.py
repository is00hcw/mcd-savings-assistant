# -*- coding: utf-8 -*-
"""省钱账本（H 模块数据源）

记录两类事件：
  drop    盯价发现的降价/新低（省 = 历史最低 - 当前价，估算值）
  order   query-order 核实的订单优惠（来自 totalDiscountAmount）

数据只存本机 ~/.mcd-skill/ledger.jsonl，不收集不上传。
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import CACHE_DIR

LEDGER_FILE = CACHE_DIR / "ledger.jsonl"
SAVINGS_SOURCE = "query-order.totalDiscountAmount"


def append_event(kind: str, **fields) -> dict:
    """追加一条账本记录；order 金额必须来自已核实的服务端订单详情。"""
    if kind not in {"drop", "order"}:
        raise ValueError(f"不支持的账本事件类型: {kind}")
    if kind == "order":
        verified = fields.get("savingsVerified") is True
        if verified:
            if fields.get("savingsSource") != SAVINGS_SOURCE:
                raise ValueError(f"实际优惠来源必须是 {SAVINGS_SOURCE}")
            amount = _strict_amount(fields.get("saved"))
            if amount is None:
                raise ValueError("已核实订单必须包含非负实际优惠金额")
            fields.pop("saved", None)
            fields["actualSaved"] = amount
        else:
            fields.pop("saved", None)
            fields.pop("actualSaved", None)
            fields["savingsVerified"] = False
    else:
        amount = _strict_amount(fields.get("saved"))
        if amount is None:
            raise ValueError("降价事件必须包含非负估算优惠金额")
        fields.pop("saved", None)
        fields["potential"] = amount
    rec = {"kind": kind, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), **fields}
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_events() -> list[dict]:
    if not LEDGER_FILE.exists():
        return []
    out = []
    for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _since(period: str, now: datetime) -> datetime | None:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    return None


def aggregate(period: str = "month", now: datetime | None = None) -> dict:
    """按周期聚合账本 → 省钱账单数据源。

    period: today | week | month | all

    口径分离（重要）：
      - saved（实际省下）＝ 仅累计来源已核实的 order 优惠；
      - potential（潜在优惠）＝ 仅累计 drop（盯价发现的降价）估算值，尚未购买。
      二者**不相加**，避免把「尚未购买的潜在优惠」误当「真实省下」。
      by_store / by_item 也只聚合 order 的真实优惠，不混入 drop 估算。
    """
    period = period if period in {"today", "week", "month", "all"} else "month"
    since = _since(period, now or datetime.now())
    evs = load_events()

    saved, potential, drops, orders, unverified_orders = 0.0, 0.0, 0, 0, 0
    by_store: dict[str, float] = {}
    by_item: dict[str, dict] = {}
    report_events: list[dict] = []
    for e in evs:
        ts = _parse_ts(e.get("ts"))
        if since:
            if ts is None or ts < since:
                continue
        kind = e.get("kind")
        if kind == "drop":
            amount = _strict_amount(e.get("potential", e.get("saved"))) or 0.0
            drops += 1
            potential += amount
            public_event = dict(e)
            public_event.pop("saved", None)
            public_event["potential"] = amount
            public_event["savingsStatus"] = "potential"
            report_events.append(public_event)
            continue          # drop 是估算，只进「潜在优惠」，不进真实省下/门店/商品榜
        if kind != "order":
            continue
        orders += 1
        amount = _strict_amount(e.get("actualSaved", e.get("saved")))
        verified = (e.get("savingsVerified") is True
                    and e.get("savingsSource") == SAVINGS_SOURCE and amount is not None)
        public_event = dict(e)
        public_event.pop("saved", None)
        public_event["savingsVerified"] = verified
        public_event["savingsStatus"] = "verified" if verified else "unverified"
        if verified:
            public_event["actualSaved"] = amount
        else:
            public_event.pop("actualSaved", None)
            unverified_orders += 1
        report_events.append(public_event)
        if not verified:
            continue
        saved += amount
        sc = str(e.get("storeCode") or "")
        if sc:
            by_store[sc] = round(by_store.get(sc, 0.0) + amount, 2)
        nm = e.get("name")
        if nm:
            rec = by_item.setdefault(nm, {"saved": 0.0, "count": 0})
            rec["saved"] = round(rec["saved"] + amount, 2)
            rec["count"] += 1

    items = [{"name": k, "price": v["saved"], "note": f'命中 {v["count"]} 次'}
             for k, v in sorted(by_item.items(), key=lambda x: -x[1]["saved"])[:10]]
    return {
        "period": period,
        "saved": round(saved, 2),
        "potential": round(potential, 2),
        "drops": drops,
        "orders": orders,
        "unverifiedOrders": unverified_orders,
        "stores": len(by_store),
        "items": items,
        "byStore": [{"storeCode": k, "price": v} for k, v in
                    sorted(by_store.items(), key=lambda x: -x[1])[:5]],
        "events": report_events[-50:],
    }


def _strict_amount(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        amount = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return round(amount, 2)


def _parse_ts(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
