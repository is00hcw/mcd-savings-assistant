# -*- coding: utf-8 -*-
"""D · 最优下单决策（双轨推荐 + OrderDraft）

硬约束（实测）：积分券每单限 1 张且不叠加 → 枚举退化为"线性挑一张"。
金额单位：calculate-price 返回=分。
takeWayCode 只能来自 calculate-price 的 takeWayList。
"""
import json

from .normalize import normalize_name
from . import price_memory


def build_items(client, store_code: str, wanted: list[tuple[str, int]]) -> list[dict]:
    """把 [(商品名或code, 数量)] 解析为 calculate-price 的 items。

    商品名会先在该店菜单内按归一化 name 查 code（code 是门店内下单凭证）。
    同时把商品名回填进 items（供确认卡/战报卡展示，calculate-price 忽略多余字段）。
    """
    menu = _menu_index(client, store_code)
    menu_codes = {k: str(m.get("code")) for k, m in menu.items() if m.get("code")}
    items = []
    for name_or_code, qty in wanted:
        code, label = name_or_code, None
        if not name_or_code.isdigit():
            key = normalize_name(name_or_code)
            code = menu_codes.get(key)
            if not code:
                # 模糊兜底：用户常说简称（"麦旋风" → 优先命中他买过的那款）
                key2, cand = _fuzzy_menu_key(key, menu)
                if key2:
                    code, label = menu_codes[key2], (menu.get(key2) or {}).get("name")
                elif len(cand) == 1:
                    key2 = cand[0]
                    code, label = menu_codes[key2], (menu.get(key2) or {}).get("name")
                else:
                    tip = ("；该店相近商品：" + "、".join(
                        (menu.get(c) or {}).get("name", c) for c in cand[:6])) if cand else ""
                    raise ValueError(
                        f"门店 {store_code} 菜单中找不到商品: {name_or_code}{tip}")
            else:
                label = (menu.get(key) or {}).get("name") or name_or_code
        item = {"productCode": code, "quantity": int(qty)}
        if label:
            item["name"] = label
        items.append(item)
    return items


def _fuzzy_menu_key(key: str, menu: dict[str, dict],
                    profile: dict | None = None) -> tuple[str, list[str]]:
    """简称兜底排序规则（按优先级）：

    1. **买过的优先**——用户历史订单里买过的款（buyCount 降序），
       避免他常点的「麦旋风任选」被陌生的「蓝莓爆爆珠麦旋风」抢走；
    2. 其次取**最短**的包含匹配（最贴近用户口中的通用名）；
    3. 仍无解时报错并列出相近商品，让用户改口。
    """
    if not key:
        return "", []
    profile = profile if profile is not None else price_memory.load_profile()
    cands = [k for k in menu if k and (k in key or key in k)]
    if not cands:
        return "", []

    def rank(k: str) -> tuple:
        bought = int((profile.get(k) or {}).get("buyCount") or 0)
        contains_query = 0 if key in k else 1     # 菜单名含用户输入的优先
        return (0 if bought > 0 else 1, -bought, contains_query, len(k), k)

    ordered = sorted(cands, key=rank)
    return ordered[0], ordered


def _menu_index(client, store_code: str) -> dict[str, dict]:
    meals_raw = client.meals(store_code)
    md = meals_raw.get("data") if isinstance(meals_raw, dict) else meals_raw
    from .normalize import meal_index_from_query_meals
    return meal_index_from_query_meals(md if isinstance(md, dict) else {})


def _coupon_candidates(client, store_code: str) -> list[dict]:
    """合并卡包券 + 门店可用券，去重。

    实测结构：query-my-coupons → data.coupons[]（id/code/title…）。
    """
    coupons: dict[str, dict] = {}
    for fetch in (lambda: client.my_coupons(),
                  lambda: client.store_coupons(store_code)):
        try:
            raw = fetch()
        except Exception:
            continue
        d = raw.get("data") if isinstance(raw, dict) else raw
        items: list = []
        if isinstance(d, list):
            items = d
        elif isinstance(d, dict):
            for k in ("coupons", "list", "couponList"):
                if isinstance(d.get(k), list):
                    items = d[k]
                    break
        for c in items:
            cid = str(c.get("id") or c.get("couponId") or "")
            if cid:
                coupons[cid] = c
    return list(coupons.values())


def calc_price(client, store_code: str, items: list[dict], coupon: dict | None = None,
               be_type: int = 1, order_type: int = 1) -> dict:
    """调 calculate-price。返回原始结果（金额=分，实测）。

    实测必带参数：beType(1-到店取餐/2-麦乐送/5-得来速/6-团餐)、
    orderType(1-到店/2-外送)、beCode("1" 可用)；takeWayList[].code 如 eat-in/locker-in。
    """
    args = {"storeCode": store_code, "items": items,
            "beType": be_type, "orderType": order_type, "beCode": "1"}
    if coupon:
        item0 = dict(items[0])
        item0["couponId"] = coupon.get("id") or coupon.get("couponId")
        item0["couponCode"] = coupon.get("code") or coupon.get("couponCode")
        args["items"] = [item0] + [dict(i) for i in items[1:]]
    return client.call("calculate-price", args)


def optimize(client, store_code: str, wanted: list[tuple[str, int]],
             track: str = "global") -> dict:
    """产出最优 OrderDraft。

    track=global: 对给定商品组合枚举券挑最低价。
    track=top3  : 先取购买量 Top3 商品里当前可买的，再同样算最低价。
    """
    if track == "top3":
        tops = price_memory.top_products(3)
        menu = _menu_index(client, store_code)
        wanted = [(k, 1) for k, _ in tops if k in menu]
        if not wanted:
            raise ValueError("Top3 常买商品在该店菜单中均不可买，改用 --track global")

    items = build_items(client, store_code, wanted)

    # ① 无券基线
    base = calc_price(client, store_code, items)
    best = {"coupon": None, "result": base}
    best_price = _price_of(base)

    # ② 线性枚举每张券各算一次（积分券限一张不叠加，无需组合）
    for c in _coupon_candidates(client, store_code):
        try:
            r = calc_price(client, store_code, items, c)
        except Exception:
            continue  # 不适用该单的券直接跳过
        p = _price_of(r)
        if p < best_price:
            best = {"coupon": c, "result": r}
            best_price = p

    take_way = _pick_take_way(best["result"])
    draft = _make_draft(store_code, items, best, take_way, track)
    return draft


def _price_of(calc_result: dict) -> float:
    d = calc_result.get("data") if isinstance(calc_result, dict) else calc_result
    if not isinstance(d, dict):
        return float("inf")
    for key in ("price", "payPrice", "realTotalAmount"):
        try:
            v = float(d.get(key))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return float("inf")


def _pick_take_way(calc_result: dict) -> dict | None:
    d = calc_result.get("data") if isinstance(calc_result, dict) else calc_result
    if not isinstance(d, dict):
        return None
    tw = d.get("takeWayList") or []
    return tw[0] if tw else None


def _fen2yuan(v) -> float:
    try:
        return round(float(v) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _make_draft(store_code: str, items: list[dict], best: dict,
                take_way: dict | None, track: str) -> dict:
    d = best["result"].get("data") if isinstance(best["result"], dict) else best["result"]
    d = d if isinstance(d, dict) else {}
    price = d.get("price")
    orig = d.get("originalPrice")
    discount = d.get("discount")
    coupon = best["coupon"] or {}
    draft = {
        "storeCode": store_code,
        "storeName": d.get("storeName") or store_code,
        "takeWay": "pickup",
        "takeWayCode": (take_way or {}).get("code") or (take_way or {}).get("takeWayCode") or "",
        "takeWayName": (take_way or {}).get("title") or (take_way or {}).get("name") or "",
        "items": items,
        "coupon": ({
            "couponId": str(coupon.get("id") or coupon.get("couponId") or ""),
            "couponCode": str(coupon.get("code") or coupon.get("couponCode") or ""),
            "name": coupon.get("title") or coupon.get("name") or coupon.get("couponName") or "",
        } if coupon else None),
        "price": _fen2yuan(price),
        "originalPrice": _fen2yuan(orig),
        "discount": _fen2yuan(discount),
        "savingDetail": (
            f"原价{_fen2yuan(orig)} - 券{_fen2yuan(discount)} = {_fen2yuan(price)}"
            if best["coupon"] else f"原价{_fen2yuan(orig)}，未用券 {_fen2yuan(price)}"
        ),
        "reservationDate": None,
        "source": ("global-lowest" if track == "global" else "top3-history"),
    }
    return draft


def draft_to_json(draft: dict) -> str:
    return json.dumps(draft, ensure_ascii=False, indent=2)
