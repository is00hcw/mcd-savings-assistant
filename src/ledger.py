# -*- coding: utf-8 -*-
"""省钱账本（H 模块数据源）

记录两类事件：
  drop    盯价发现的降价/新低（省 = 历史最低 - 当前价，估算值）
  order   实际确认下单后的真实优惠（省 = 原价 - 实付，精确值）

数据只存本机 ~/.mcd-skill/ledger.jsonl，不收集不上传。
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import CACHE_DIR

LEDGER_FILE = CACHE_DIR / "ledger.jsonl"


def append_event(kind: str, **fields) -> dict:
    """追加一条账本记录。kind: drop（盯价发现）| order（真实下单）。"""
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
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _since(days: int) -> datetime | None:
    return datetime.now() - timedelta(days=days)


def aggregate(period: str = "month") -> dict:
    """按周期聚合账本 → 省钱账单数据源。

    period: today | week | month | all
    """
    span = {"today": 1, "week": 7, "month": 30, "all": 36500}.get(period, 30)
    since = _since(span)
    evs = load_events()

    saved, drops, orders = 0.0, 0, 0
    by_store: dict[str, float] = {}
    by_item: dict[str, dict] = {}
    for e in evs:
        ts = _parse_ts(e.get("ts"))
        if since and ts and ts < since:
            continue
        kind = e.get("kind")
        amount = _num(e.get("saved"))
        if kind == "drop":
            drops += 1
        elif kind == "order":
            orders += 1
            saved += amount
        saved += 0 if kind == "order" else amount
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
        "drops": drops,
        "orders": orders,
        "stores": len(by_store),
        "items": items,
        "byStore": [{"storeCode": k, "price": v} for k, v in
                    sorted(by_store.items(), key=lambda x: -x[1])[:5]],
        "events": [e for e in evs if not since or not _parse_ts(e.get("ts"))
                   or _parse_ts(e["ts"]) >= since][-50:],
    }


def _num(v) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _parse_ts(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None