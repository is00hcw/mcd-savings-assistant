# -*- coding: utf-8 -*-
"""D · 最优下单决策（单轨 global/top3 + 双轨候选 dual + OrderDraft）

硬约束（实测）：积分券每单限 1 张且不叠加 → 枚举退化为"线性挑一张"。
金额单位：calculate-price 返回=分。
takeWayCode 只能来自 calculate-price 的 takeWayList（到店 orderType=1 才有）。

取餐范围：
  · 到店自提（默认）：beType=1 / orderType=1，takeWayCode 从 takeWayList 取；
  · 麦乐送外卖：beType=2 / orderType=2，必传 beCode（delivery-query-stores 返回），
    无 takeWayList、不传 takeWayCode，价格含配送费(deliveryPrice)+打包费(packingPrice)。

轨道选择：
  · --track global / top3：单轨直出一条候选；
  · --track dual（双轨候选二选一）：optimize_dual 同时算两条，confirm_candidates
    出双候选确认卡，用户选定一条后再走单卡确认闸。

reservationDate：本工具产出的都是**即时单**（定时任务=到点重算价+提醒+人工确认，
不做预约单），故恒为 None；字段保留以兼容 create-order 的预约能力与确认闸摘要。
"""
from __future__ import annotations

import json
import time

from .normalize import normalize_name
from .mcd_client import McpError, NetworkError
from . import price_memory


def build_items(client, store_code: str, wanted: list[tuple[str, int]],
                be_type: int = 1, order_type: int = 1,
                be_code: str | None = None) -> list[dict]:
    """把 [(商品名或code, 数量)] 解析为 calculate-price 的 items。

    商品名会先在该店菜单内按归一化 name 查 code（code 是门店内下单凭证）。
    同时把商品名回填进 items（供确认卡/分享卡展示，calculate-price 忽略多余字段）。
    be_type/order_type/be_code：外卖(2/2)场景查菜单必传，否则 query-meals 报错。
    """
    menu = _menu_index(client, store_code, be_type=be_type, order_type=order_type,
                       be_code=be_code)
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


def _menu_index(client, store_code: str, be_type: int = 1, order_type: int = 1,
                be_code: str | None = None) -> dict[str, dict]:
    meals_raw = client.meals(store_code, order_type=order_type, be_type=be_type,
                             be_code=be_code)
    md = meals_raw.get("data") if isinstance(meals_raw, dict) else meals_raw
    from .normalize import meal_index_from_query_meals
    return meal_index_from_query_meals(md if isinstance(md, dict) else {})


def _coupon_candidates(client, store_code: str, be_type: int = 1,
                       order_type: int = 1, be_code: str | None = None) -> tuple[list[dict], list[str]]:
    """合并卡包券 + 门店可用券，去重。

    返回 (券列表, 警告列表)。某一路查询失败不再静默吞掉，
    而是记入 warnings 让确认卡标注「结果可能不完整」。
    门店券 query-store-coupons 必传 orderType/beType（外送再传 beCode）。
    实测结构：query-my-coupons → data.coupons[]（id/code/title…）。
    """
    coupons: dict[str, dict] = {}
    warnings: list[str] = []
    for label, fetch in (("卡包券", lambda: client.my_coupons()),
                         ("门店券", lambda: client.store_coupons(
                             store_code, order_type=order_type, be_type=be_type,
                             be_code=be_code))):
        try:
            raw = fetch()
        except Exception as e:
            warnings.append(f"{label}读取失败: {e}")
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
    return list(coupons.values()), warnings


def calc_price(client, store_code: str, items: list[dict], coupon: dict | None = None,
               be_type: int = 1, order_type: int = 1, be_code: str | None = None) -> dict:
    """调 calculate-price。返回原始结果（金额=分，实测）。

    实测必带参数：beType(1-到店取餐/2-麦乐送/5-得来速/6-团餐)、
    orderType(1-到店/2-外送)、beCode("1" 可用)；takeWayList[].code 如 eat-in/locker-in。
    外卖(beType=2) 必传 beCode，且结果无 takeWayList。
    """
    args = {"storeCode": store_code, "items": items,
            "beType": be_type, "orderType": order_type}
    if be_type == 1:
        args["beCode"] = "1"
    elif be_code:
        args["beCode"] = be_code
    if coupon:
        item0 = dict(items[0])
        item0["couponId"] = coupon.get("id") or coupon.get("couponId")
        item0["couponCode"] = coupon.get("code") or coupon.get("couponCode")
        args["items"] = [item0] + [dict(i) for i in items[1:]]
    return client.call("calculate-price", args)


def optimize(client, store_code: str, wanted: list[tuple[str, int]],
             track: str = "global", delivery: dict | None = None,
             coupon: str = "auto") -> dict:
    """产出最优 OrderDraft。

    track=global: 对给定商品组合枚举券挑最低价。
    track=top3  : 先取购买量 Top3 商品里当前可买的，再同样算最低价。
    delivery    : None=到店自提；dict={"addressId","beCode","addressText"} 外卖。
    coupon      : "auto"（默认，枚举所有候选券取最低价）；或具体券 id/code——
                  只测「无券基线 + 该券」两条，不再全量枚举；该券不可用时
                  记入 pricingWarnings 并注明其生效时间窗，退回无券价。
    """
    be_type = 2 if delivery else 1
    order_type = 2 if delivery else 1
    be_code = (delivery or {}).get("beCode")

    if track == "top3":
        tops = price_memory.top_products(3)
        menu = _menu_index(client, store_code, be_type=be_type, order_type=order_type,
                           be_code=be_code)
        wanted = [(k, 1) for k, _ in tops if k in menu]
        if not wanted:
            raise ValueError("Top3 常买商品在该店菜单中均不可买，改用 --track global")

    items = build_items(client, store_code, wanted, be_type=be_type, order_type=order_type,
                        be_code=be_code)
    return _best_quote(client, store_code, items, be_type=be_type,
                       order_type=order_type, be_code=be_code,
                       coupon=coupon, track=track)


def _match_coupon(coupons: list[dict], key: str) -> dict | None:
    """按券 id / code 匹配指定券（大小写不敏感）。"""
    k = str(key).strip()
    if not k:
        return None
    kl = k.lower()
    for c in coupons:
        for field in ("id", "couponId", "code", "couponCode"):
            v = c.get(field)
            if v is not None and str(v).strip().lower() == kl:
                return c
    return None


def _coupon_validity_text(c: dict) -> str:
    """人类可读的券生效时间窗（字段名多源兜底，拿不到就明说未知）。"""
    start = (c.get("tradeStartDateTime") or c.get("tradeStartDate")
             or c.get("validFrom") or c.get("startDate") or "")
    end = (c.get("tradeEndDateTime") or c.get("tradeEndDate")
           or c.get("validTo") or c.get("endDate") or "")
    start, end = str(start)[:16], str(end)[:16]
    if start and end:
        return f"生效 {start} ~ {end}"
    if start:
        return f"{start} 起生效"
    if end:
        return f"有效期至 {end}"
    return "生效时间未知（以券面为准）"


def _best_quote(client, store_code: str, items: list[dict],
                be_type: int = 1, order_type: int = 1, be_code: str | None = None,
                coupon: str = "auto", track: str = "global") -> dict:
    """对给定 items 枚举券取最优价并生成草稿（optimize / reprice_draft 共用）。

    coupon 语义见 optimize()。指定券时算价失败不再静默：把失败原因（含券的
    生效时间窗）写进 warnings，让确认卡明示「指定券为何没生效」，退回无券价。
    """
    # ① 无券基线（必要查询：失败/无金额必须报错，绝不产出误导草稿）
    base = calc_price(client, store_code, items,
                      be_type=be_type, order_type=order_type, be_code=be_code)
    best = {"coupon": None, "result": base}
    best_price = _price_of(base)

    # ② 线性枚举每张券各算一次（积分券限一张不叠加，无需组合）
    #    业务错误（如「当前优惠券不可用」「促销规则不支持该商品」）是正常的券过滤，
    #    静默跳过；网络错误/未知异常才算「结果可能不完整」记入 warnings。
    coupons, warnings = _coupon_candidates(client, store_code,
                                           be_type=be_type, order_type=order_type,
                                           be_code=be_code)
    specified_key = str(coupon or "").strip()
    specified = bool(specified_key) and specified_key.lower() != "auto"
    picked = _match_coupon(coupons, specified_key) if specified else None
    if specified and picked is None:
        warnings.append(f"指定券 {specified_key} 不在卡包/门店可用券中，已按无券价计")
    candidates = [picked] if picked else ([] if specified else coupons)

    for c in candidates:
        cid = str(c.get("id") or c.get("couponId") or "?")
        ccode = str(c.get("code") or c.get("couponCode") or "")
        disp = f"{ccode}({cid})" if ccode else cid
        try:
            r = calc_price(client, store_code, items, c,
                           be_type=be_type, order_type=order_type, be_code=be_code)
        except NetworkError as e:
            if specified:
                warnings.append(f"指定券 {disp} 算价网络错误，已按无券价计：{e}")
            else:
                warnings.append(f"券 {cid} 算价网络错误: {e}")
            continue
        except McpError as e:
            if specified:
                # 用户点名要用的券不可用必须说明原因（如「券没到可用时间」→ 生效窗口）
                warnings.append(f"指定券 {disp} 当前不可用"
                                f"（{_coupon_validity_text(c)}），已按无券价计：{e}")
            continue  # 业务错误：券不适用当前订单，正常过滤，不算「结果不完整」
        except Exception as e:
            warnings.append(f"券 {cid} 算价失败: {e}")
            continue
        p = _price_of(r)
        if p < best_price:
            best = {"coupon": c, "result": r}
            best_price = p

    take_way = _pick_take_way(best["result"])
    delivery_ctx = None
    if be_type == 2:
        delivery_ctx = {"addressId": None, "beCode": be_code, "addressText": None}
    draft = _make_draft(store_code, items, best, take_way, track, delivery=delivery_ctx)
    _attach_product_breakdown(draft, best["result"])
    if warnings:
        draft["pricingWarnings"] = warnings
    return draft


_DISPLAY_ONLY_ITEM_FIELDS = ("unitPrice", "subtotal", "originalSubtotal", "officialName")
_LINE_IDENTITY_FIELDS = (
    "id", "lineId", "itemId", "skuId", "skuCode", "specId", "specCode",
    "spec", "specification", "specName", "roundList", "rounds",
    "modification", "modifications", "options", "optionList",
    "replaceList", "replacementList", "subItems", "setMealItems",
)


def _quantity_key(value, default: bool = False) -> str:
    if value in (None, ""):
        return "1" if default else ""
    try:
        number = float(value)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError, OverflowError):
        pass
    return str(value)


def _line_group_key(item: dict, input_row: bool = False) -> tuple[str, str]:
    return (str(item.get("productCode") or ""),
            _quantity_key(item.get("quantity"), default=input_row))


def _identity_value(value):
    if value in (None, "", [], {}):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _line_identity(item: dict) -> dict[str, str] | None:
    identity = {}
    for field in _LINE_IDENTITY_FIELDS:
        if field not in item:
            continue
        value = _identity_value(item.get(field))
        if value is not None:
            identity[field] = value
    return identity or None


def _identities_compatible(left: dict[str, str] | None,
                           right: dict[str, str] | None) -> bool:
    if not left or not right:
        return False
    common = set(left) & set(right)
    return bool(common) and all(left[field] == right[field] for field in common)


def _match_product_rows(items: list[dict], rows: list[dict]) -> list[dict | None]:
    """按商品码/数量及可验证规格身份匹配服务端行，拒绝顺序猜测。"""
    matches: list[dict | None] = [None] * len(items)
    item_groups: dict[tuple[str, str], list[int]] = {}
    row_groups: dict[tuple[str, str], list[int]] = {}
    rows_by_code: dict[str, list[int]] = {}

    for index, item in enumerate(items):
        item_groups.setdefault(_line_group_key(item, input_row=True), []).append(index)
    for index, row in enumerate(rows):
        code = str(row.get("productCode") or "")
        if not code:
            continue
        row_groups.setdefault(_line_group_key(row), []).append(index)
        rows_by_code.setdefault(code, []).append(index)

    for group, item_indexes in item_groups.items():
        candidate_indexes = row_groups.get(group, [])
        if not candidate_indexes:
            code = group[0]
            code_rows = rows_by_code.get(code, [])
            if (len(item_indexes) == 1 and len(code_rows) == 1 and
                    _quantity_key(rows[code_rows[0]].get("quantity")) == ""):
                candidate_indexes = code_rows
            else:
                continue
        if len(candidate_indexes) != len(item_indexes):
            continue

        if len(item_indexes) == 1:
            item_identity = _line_identity(items[item_indexes[0]])
            row_identity = _line_identity(rows[candidate_indexes[0]])
            if item_identity and row_identity and not _identities_compatible(
                    item_identity, row_identity):
                continue
            matches[item_indexes[0]] = rows[candidate_indexes[0]]
            continue

        # Duplicate code + quantity requires a unique two-sided identity match.
        candidates_by_item = {}
        for item_index in item_indexes:
            item_identity = _line_identity(items[item_index])
            if not item_identity:
                candidates_by_item[item_index] = []
                continue
            candidates_by_item[item_index] = [
                row_index for row_index in candidate_indexes
                if _identities_compatible(item_identity, _line_identity(rows[row_index]))
            ]
        if (any(len(candidates) != 1 for candidates in candidates_by_item.values()) or
                len({candidates[0] for candidates in candidates_by_item.values()}) !=
                len(item_indexes)):
            continue
        for item_index, candidates in candidates_by_item.items():
            matches[item_index] = rows[candidates[0]]
    return matches


def _attach_product_breakdown(draft: dict, calc_result: dict):
    """把 calculate-price 的 productList（单价/小计）回填进草稿商品行。

    让确认卡与到点消息能逐件列出「名称 × 数量 · 单价 · 小计」，
    用户一眼就能核对，而不只是看一个总价。
    """
    d = calc_result.get("data") if isinstance(calc_result, dict) else calc_result
    d = d if isinstance(d, dict) else {}
    plist = d.get("productList") or []
    items = draft.get("items", [])

    for item in items:
        for field in _DISPLAY_ONLY_ITEM_FIELDS:
            item.pop(field, None)

    matches = _match_product_rows(items, plist)
    for it, p in zip(items, matches):
        if not p:
            continue
        try:
            qty = int(float(p.get("quantity") or it.get("quantity") or 1))
        except (TypeError, ValueError, OverflowError):
            qty = 1
        sub = p.get("subtotal")
        orig_sub = p.get("originalSubtotal")
        it["unitPrice"] = _fen2yuan(sub) / qty if sub not in (None, "") else None
        it["subtotal"] = _fen2yuan(sub)
        if orig_sub not in (None, ""):
            it["originalSubtotal"] = _fen2yuan(orig_sub)
        if p.get("productName") and p["productName"] != it.get("name"):
            # 官方名更精确（如「薯条」实为「中薯条」），但保留用户原话避免看着陌生
            it["officialName"] = p["productName"]
    if d.get("storeName"):
        draft["storeName"] = d["storeName"]
    elif not draft.get("storeName") or draft["storeName"] == draft.get("storeCode"):
        cached = price_memory.get_store_name(draft.get("storeCode") or "")
        if cached:
            draft["storeName"] = cached


def _price_of(calc_result: dict) -> float:
    """从算价结果提取可识别金额（分）。找不到有效金额直接抛错，而非返回 inf。"""
    return float(_price_value(calc_result))


def _price_value(calc_result: dict):
    """提取草稿和比较阶段共用的原始金额字段（单位：分）。"""
    d = calc_result.get("data") if isinstance(calc_result, dict) else calc_result
    if not isinstance(d, dict):
        raise ValueError("算价结果无 data 字段，无法提取金额")
    for key in ("price", "payPrice", "realTotalAmount"):
        try:
            raw = d.get(key)
            v = float(raw)
            if v > 0:
                return raw
        except (TypeError, ValueError):
            continue
    raise ValueError("算价结果缺少可识别的金额字段（price/payPrice/realTotalAmount）")


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
                take_way: dict | None, track: str, delivery: dict | None = None) -> dict:
    d = best["result"].get("data") if isinstance(best["result"], dict) else best["result"]
    d = d if isinstance(d, dict) else {}
    price = _price_value(best["result"])
    orig = d.get("originalPrice")
    discount = d.get("discount")
    coupon = best["coupon"] or {}
    is_delivery = bool(delivery)

    # 硬校验：金额缺失 → 直接报错；取餐方式仅到店必填（外卖无 takeWayList）
    take_way_code = (take_way or {}).get("code") or (take_way or {}).get("takeWayCode") or ""
    if not is_delivery and not take_way_code:
        raise ValueError("算价结果缺少取餐方式（takeWayList 为空），无法生成可下单草稿")
    yuan = _fen2yuan(price)
    if yuan <= 0:
        raise ValueError("算价结果缺少有效金额，无法生成草稿")

    draft = {
        "storeCode": store_code,
        "storeName": d.get("storeName") or store_code,
        "takeWay": "delivery" if is_delivery else "pickup",
        "takeWayCode": take_way_code,
        "takeWayName": (take_way or {}).get("title") or (take_way or {}).get("name") or
                       ("麦乐送" if is_delivery else ""),
        "orderType": 2 if is_delivery else 1,
        "beType": 2 if is_delivery else 1,
        "beCode": (delivery or {}).get("beCode") or "",
        "addressId": (delivery or {}).get("addressId") or "",
        "addressText": (delivery or {}).get("addressText") or "",
        "deliveryFee": _fen2yuan(d.get("deliveryPrice")),
        "packingFee": _fen2yuan(d.get("packingPrice")),
        "quotedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "coupon": ({
            "couponId": str(coupon.get("id") or coupon.get("couponId") or ""),
            "couponCode": str(coupon.get("code") or coupon.get("couponCode") or ""),
            "name": coupon.get("title") or coupon.get("name") or coupon.get("couponName") or "",
            "expireAt": (coupon.get("tradeEndDateTime") or coupon.get("tradeEndDate") or ""),
        } if coupon else None),
        "price": _fen2yuan(price),
        "originalPrice": _fen2yuan(orig),
        "discount": _fen2yuan(discount),
        "savingDetail": (
            f"原价{_fen2yuan(orig)} - 券{_fen2yuan(discount)} = {_fen2yuan(price)}"
            if best["coupon"] else f"原价{_fen2yuan(orig)}，未用券 {_fen2yuan(price)}"
        ),
        # 即时单（定时任务亦然：到点提醒+人工确认，不做预约单），字段仅为兼容预留
        "reservationDate": None,
        "source": ("global-lowest" if track == "global" else "top3-history"),
    }
    return draft


def draft_to_json(draft: dict) -> str:
    return json.dumps(draft, ensure_ascii=False, indent=2)


def _mirror_item_display_fields(fresh: dict, original: dict) -> None:
    """让重算草稿的 item 展示字段与原草稿逐项一致，避免 diff 误报。

    unitPrice / subtotal / originalSubtotal / officialName 由 _attach_product_breakdown
    从**本次** calculate-price 的 productList 回填，只服务于确认卡/分享卡展示，
    却被 _items_fingerprint 计入「商品清单」指纹。于是原草稿没有这些字段、或本次
    productList 缺失/金额不同，都会凭空 diff 出「商品清单已变化」，把本该放行的
    下单误拦——带套餐规格的定制商品首当其冲。

    重算草稿只用于和「用户看过的那份草稿」比对价格/券/规格是否变了（cmd_order 里
    diff 通过后仍执行原草稿），故其展示字段一律以原草稿为准：原草稿没有就不补，
    原草稿有就照抄，绝不参与 diff。商品规格（roundList / modification / 替换项 等）
    不在此列，任何变化都必须如实呈现。
    """
    fresh_items = fresh.get("items") or []
    old_items = original.get("items") or []
    matches = _match_product_rows(fresh_items, old_items)
    for it, old_item in zip(fresh_items, matches):
        if old_item is None:
            # 同码同量且没有可信身份时，不能把旧展示金额按顺序套到新行。
            for field in _DISPLAY_ONLY_ITEM_FIELDS:
                it.pop(field, None)
            continue
        for f in _DISPLAY_ONLY_ITEM_FIELDS:
            if f in old_item:
                it[f] = old_item[f]
            else:
                it.pop(f, None)


def reprice_draft(client, draft: dict) -> dict:
    """确认前重新算价：按草稿的 store/items 重新最优算价，返回最新 draft。

    下单前把「用户看过的那份草稿」与「此刻重新算出的草稿」比对，
    价格/券/商品有变化就拒绝并重新展示（见 order_exec.diff_draft）。

    ⚠️ 原始 items 的完整结构（roundList / modification / 替换项 / 套餐规格等
    服务端要求的扩展字段）必须**原样透传**给 calculate-price 并保留在新草稿里：
    以前按 (name, quantity) 重建 items 会丢失这些字段，导致带套餐规格的定制
    商品重算价后与原草稿 diff 出「商品清单已变化」，被确认闸误拦、无法下单。
    只更新服务端重新报价后确实变化的价格 / 券 / 取餐方式；
    reservationDate / addressId / addressText 等用户既定字段原样保留。
    """
    items = json.loads(json.dumps(draft.get("items") or []))   # 深拷贝，避免污染原草稿
    if not items:
        raise ValueError("草稿缺少商品清单，无法重新算价")
    delivery = None
    if draft.get("orderType") == 2 or draft.get("takeWay") == "delivery":
        delivery = {
            "addressId": draft.get("addressId"),
            "beCode": draft.get("beCode"),
            "addressText": draft.get("addressText"),
        }
    fresh = _best_quote(client, draft["storeCode"], items,
                        be_type=2 if delivery else 1,
                        order_type=2 if delivery else 1,
                        be_code=(delivery or {}).get("beCode"))
    # 用户既定字段不参与「重新报价」：保留原值，避免 diff 误报变化
    fresh["reservationDate"] = draft.get("reservationDate")
    fresh["addressId"] = draft.get("addressId") or ""
    fresh["addressText"] = draft.get("addressText") or ""
    _mirror_item_display_fields(fresh, draft)
    return fresh


def optimize_dual(client, store_code: str, wanted: list[tuple[str, int]],
                  delivery: dict | None = None) -> dict:
    """双轨候选：同时算「全局最低」与「常买 Top3」两条候选，供用户二选一。

    返回 {"global": draft, "top3": draft | None, "warnings": [...]}。
    top3 轨道失败（如常买商品该店不可买）不拖垮 global：降级为单候选并记 warnings。
    """
    g = optimize(client, store_code, wanted, track="global", delivery=delivery)
    warnings: list[str] = []
    t = None
    try:
        t = optimize(client, store_code, wanted, track="top3", delivery=delivery)
    except (ValueError, McpError) as e:
        warnings.append(f"常买Top3 轨道不可用，仅有全局最低候选：{e}")
    return {"global": g, "top3": t, "warnings": warnings}


def _store_open(s: dict) -> bool:
    """businessStatus 宽容解析：字段缺失/未知不误杀，明确的「休息/停业」才排除。

    实测 CLI 展示口径：businessStatus 真值=营业中。为稳妥按类型分派：
    布尔/数值按真假；字符串含「休/close/rest」或为 "0"/"false" 视为休息，
    其余（如「营业中」「open」）视为营业中。
    """
    st = s.get("businessStatus")
    if st in (None, ""):
        return True
    if isinstance(st, bool):
        return st
    if isinstance(st, (int, float)):
        return st != 0
    t = str(st).strip().lower()
    if not t or t in ("1", "true"):
        return True
    if t in ("0", "false") or "休" in t or "close" in t or "rest" in t:
        return False
    return True


def resolve_delivery(client, address_id: str, store_code: str | None = None,
                     be_code: str | None = None) -> dict:
    """外卖前置：给定配送地址，解析出可下单的 (storeCode, beCode, addressText)。

    返回 {"addressId","storeCode","beCode","addressText","storeName"}。
    - 指定了 store_code + be_code → 校验该门店确实配送该地址、beCode 与接口
      一致（防无效组合直送算价接口），通过后直接用并回填地址文本；
    - 只指定 store_code → 从可配送门店按 storeCode 匹配 beCode（休息中报错）；
    - 都不指定 → 取第一个**营业中**的可配送门店。
    """
    if not address_id:
        raise ValueError("外卖需要配送地址 addressId")

    # 地址文本（拿不到不致命，beCode/门店才是下单关键）
    address_text = ""
    try:
        raw = client.delivery_addresses()
        d = raw.get("data") if isinstance(raw, dict) else raw
        addrs = (d or {}).get("addresses") or [] if isinstance(d, dict) else []
        for a in addrs:
            if str(a.get("addressId")) == str(address_id):
                address_text = (a.get("fullAddress") or "").strip()
                break
    except Exception:
        pass

    def _fetch_stores() -> list[dict]:
        raw = client.delivery_stores(address_id)
        d = raw.get("data") if isinstance(raw, dict) else raw
        stores = d if isinstance(d, list) else (d or {}).get("list") \
            or (d or {}).get("stores") or []
        return [s for s in stores if isinstance(s, dict)]

    if be_code and store_code:
        # 统一校验「门店 / 业务码 / 配送地址」三者关系：门店必须配送该地址，
        # 且接口给出的 beCode 与传入一致（不一致说明业务码过期或张冠李戴）
        stores = _fetch_stores()
        hit = next((s for s in stores
                    if str(s.get("storeCode")) == str(store_code)), None)
        if hit is None:
            names = "、".join(f"{s.get('storeName')}({s.get('storeCode')})"
                              for s in stores[:6])
            raise ValueError(f"门店 {store_code} 不配送该地址；可配送门店：{names}")
        listed = str(hit.get("beCode") or "")
        if not listed:
            raise ValueError(f"门店 {store_code} 查询结果缺少 beCode，无法验证配送业务编码")
        if listed != str(be_code):
            raise ValueError(f"门店 {store_code} 在该地址的业务编码为 {listed}，"
                             f"与传入 beCode={be_code} 不一致，请重新用 delivery 查询")
        if not _store_open(hit):
            raise ValueError(f"门店 {store_code} 当前休息中，暂不可外卖配送")
        return {"addressId": str(address_id), "storeCode": str(store_code),
                "beCode": str(be_code), "addressText": address_text, "storeName": ""}

    stores = _fetch_stores()
    if not stores:
        raise ValueError("该配送地址暂无可配送门店")

    chosen = None
    if store_code:
        for s in stores:
            if str(s.get("storeCode")) == str(store_code):
                chosen = s
                break
        if not chosen:
            names = "、".join(f"{s.get('storeName')}({s.get('storeCode')})" for s in stores[:6])
            raise ValueError(f"门店 {store_code} 不配送该地址；可配送门店：{names}")
        if not _store_open(chosen):
            raise ValueError(f"门店 {store_code} 当前休息中，暂不可外卖配送")
    else:
        open_stores = [s for s in stores if _store_open(s)]
        if not open_stores:
            raise ValueError("该配送地址的可配送门店均休息中，暂不可外卖下单")
        chosen = open_stores[0]

    resolved_be = str(chosen.get("beCode") or "")
    if not resolved_be:
        raise ValueError(f"可配送门店 {chosen.get('storeCode')} 缺少 beCode，无法算价")
    return {"addressId": str(address_id), "storeCode": str(chosen.get("storeCode")),
            "beCode": resolved_be, "addressText": address_text,
            "storeName": chosen.get("storeName") or ""}
