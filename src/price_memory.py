# -*- coding: utf-8 -*-
"""A · 低价记忆（历史最低优惠价基线）

数据源：query-meals 的 currentPrice（实测为优惠价）。
基线文件：~/.mcd-skill/price_baseline.json，key=归一化 name。
冷启动：order-list → query-order 用 totalDiscountAmount 判定"买过优惠"作佐证
（优惠为订单级，单品实付不可得，故只记佐证不记价格）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import CACHE_DIR
from .normalize import normalize_name

BASELINE_FILE = CACHE_DIR / "price_baseline.json"
PROFILE_FILE = CACHE_DIR / "product_profile.json"
STORE_NAMES_FILE = CACHE_DIR / "store_names.json"
PROCESSED_ORDERS_FILE = CACHE_DIR / "processed_order_ids.json"
SNAPSHOT_DIR = CACHE_DIR / "meals_snapshot"
SNAPSHOT_TTL = 6 * 3600  # 菜单快照 6 小时


# ---------------------------------------------------------------------------
# 门店名缓存（query-meals / calculate-price 都不返回门店名，
# 只能从 order-list / query-nearby-stores 顺手记住）
# ---------------------------------------------------------------------------
def remember_store_names(pairs: dict) -> int:
    """记住 storeCode → storeName。pairs: {storeCode: storeName}"""
    pairs = {str(k): v for k, v in (pairs or {}).items() if k and v}
    if not pairs:
        return 0
    cache = _load(STORE_NAMES_FILE, {})
    n = 0
    for code, name in pairs.items():
        if cache.get(code) != name:
            cache[code] = name
            n += 1
    if n:
        _save(STORE_NAMES_FILE, cache)
    return n


def get_store_name(store_code: str) -> str:
    return (_load(STORE_NAMES_FILE, {}) or {}).get(str(store_code), "")


def _load(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 低价基线
# ---------------------------------------------------------------------------
def load_baseline() -> dict:
    return _load(BASELINE_FILE, {})


def update_from_meals(meal_index: dict[str, dict], store_code: str = "") -> list[dict]:
    """用一次 query-meals 快照刷新基线。返回本次发现的降价/新低事件列表。"""
    baseline = load_baseline()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    events = []
    for key, m in meal_index.items():
        try:
            cur = float(m.get("currentPrice"))
            orig = float(m.get("originalPrice") or 0)
        except (TypeError, ValueError):
            continue
        if cur <= 0:
            continue
        is_discount = orig > 0 and cur < orig  # 无折扣品（现价=原价）不入事件
        rec = baseline.get(key)
        if rec is None:
            baseline[key] = {
                "minCurrentPrice": cur, "originalPrice": orig,
                "firstSeen": now, "lastSeen": now,
                "seenStores": [store_code] if store_code else [],
            }
            continue
        prev_min = float(rec.get("minCurrentPrice") or 0)
        if is_discount and prev_min and cur < prev_min:
            events.append({
                "type": "new_low", "name": m.get("name") or key,
                "prevMin": prev_min, "currentPrice": cur,
                "originalPrice": orig, "storeCode": store_code,
            })
            rec["minCurrentPrice"] = cur
        elif is_discount and prev_min and cur == prev_min:
            events.append({
                "type": "at_low", "name": m.get("name") or key,
                "prevMin": prev_min, "currentPrice": cur,
                "originalPrice": orig, "storeCode": store_code,
            })
        rec["lastSeen"] = now
        if orig and not rec.get("originalPrice"):
            rec["originalPrice"] = orig
        if store_code and store_code not in rec.setdefault("seenStores", []):
            rec["seenStores"].append(store_code)
    _save(BASELINE_FILE, baseline)
    return events


def cold_start_from_orders(client) -> dict:
    """冷启动热身：order-list → query-order，统计"买过优惠"佐证 + 购买量。

    返回 {profile, discounted_names}。
    """
    profile = load_profile()
    has_processed_ledger = PROCESSED_ORDERS_FILE.exists()
    has_existing_counts = False
    for rec in profile.values():
        if not isinstance(rec, dict):
            continue
        try:
            has_existing_counts = int(rec.get("buyCount") or 0) > 0
        except (TypeError, ValueError):
            continue
        if has_existing_counts:
            break
    legacy_profile = not has_processed_ledger and has_existing_counts
    processed = set(str(x) for x in (_load(PROCESSED_ORDERS_FILE, []) or []))
    processed_now = set(processed)
    discounted: set[str] = set()
    order_stores: list[str] = []   # 订单里出现过的门店（按频次），用于推导常去门店
    try:
        orders = client.order_list()
    except Exception as e:
        return {"profile": profile, "discounted_names": [], "orderStores": [], "error": str(e)}
    order_items = orders.get("data") if isinstance(orders, dict) else orders
    lst = _extract_orders(order_items)
    for od in lst:
        sc = od.get("storeCode")
        if sc and sc not in order_stores:
            order_stores.append(str(sc))
        if sc and od.get("storeName"):
            remember_store_names({sc: od["storeName"]})
        oid = od.get("orderId") or od.get("orderNo") or od.get("id")
        if not oid:
            continue
        oid = str(oid)
        if oid in processed:
            continue
        if legacy_profile:
            processed_now.add(oid)
            continue
        try:
            detail = client.query_order(oid)
        except Exception:
            continue
        dd = detail.get("data") if isinstance(detail, dict) else detail
        if not isinstance(dd, dict):
            continue
        processed_now.add(oid)
        try:
            has_discount = float(dd.get("totalDiscountAmount") or 0) > 0
        except (TypeError, ValueError):
            has_discount = False
        for p in dd.get("orderProductList") or dd.get("products") or []:
            pname = p.get("productName") or p.get("name") or ""
            key = normalize_name(pname)
            if not key:
                continue
            rec = profile.setdefault(key, {"aliases": [], "buyCount": 0, "storeCodes": []})
            rec["buyCount"] += int(p.get("quantity") or 1)
            if has_discount:
                discounted.add(key)
    save_profile(profile)
    if processed_now != processed:
        _save(PROCESSED_ORDERS_FILE, sorted(processed_now))
    return {"profile": profile, "discounted_names": sorted(discounted),
            "orderStores": order_stores}


def _extract_orders(x):
    if isinstance(x, dict):
        for k in ("list", "orders", "orderList", "records"):
            if isinstance(x.get(k), list):
                return x[k]
        return []
    return x if isinstance(x, list) else []


# ---------------------------------------------------------------------------
# 商品档案（购买量统计，Top3 个性化推荐用）
# ---------------------------------------------------------------------------
def load_profile() -> dict:
    return _load(PROFILE_FILE, {})


def save_profile(profile: dict):
    _save(PROFILE_FILE, profile)


def merge_profile_from_meals(meal_index: dict[str, dict], store_code: str):
    """把菜单里见到的商品登记进档案（补 aliases/seenStores，不动 buyCount）。"""
    profile = load_profile()
    changed = False
    for key, m in meal_index.items():
        rec = profile.setdefault(key, {"aliases": [], "buyCount": 0, "storeCodes": []})
        name = m.get("name") or ""
        if name and name not in rec["aliases"] and normalize_name(name) == key:
            if rec["aliases"] and name != rec["aliases"][0]:
                rec["aliases"].append(name)
                changed = True
            elif not rec["aliases"]:
                rec["aliases"].append(name)
                changed = True
        if store_code and store_code not in rec.setdefault("storeCodes", []):
            rec["storeCodes"].append(store_code)
            changed = True
    if changed:
        save_profile(profile)


def top_products(n: int = 3) -> list[tuple[str, int]]:
    profile = load_profile()
    items = [(k, int(v.get("buyCount") or 0)) for k, v in profile.items()]
    items.sort(key=lambda x: -x[1])
    return [(k, c) for k, c in items if c > 0][:n]


# ---------------------------------------------------------------------------
# 菜单快照（降价追踪用）
# ---------------------------------------------------------------------------
def snapshot_path(store_code: str) -> Path:
    return SNAPSHOT_DIR / f"{store_code}.json"


def load_snapshot(store_code: str) -> dict | None:
    p = snapshot_path(store_code)
    snap = _load(p, None)
    if not isinstance(snap, dict):
        return None
    ts = snap.get("ts") or 0
    if time.time() - ts > SNAPSHOT_TTL:
        return None
    return snap


def save_snapshot(store_code: str, meal_index: dict[str, dict]):
    _save(snapshot_path(store_code), {
        "ts": time.time(),
        "storeCode": store_code,
        "meals": {k: {
            "name": m.get("name"), "code": m.get("code"),
            "currentPrice": m.get("currentPrice"),
            "originalPrice": m.get("originalPrice"),
            "discountType": m.get("discountType"),
        } for k, m in meal_index.items()},
    })
