# -*- coding: utf-8 -*-
"""人设话术层：「老大」人格

三原则：① 干脆有汇报感 ② 把预计优惠和已核实优惠分开报数 ③ 到点有画面感。
确认闸话术不弱化红线。
"""
from .config import get_config


def nick() -> str:
    try:
        return get_config().nickname
    except SystemExit:
        return "老大"


def greeting() -> str:
    return f"{nick()}，我上线了！先把你的历史订单导进来建个低价档案，稍等……"


def on_price_drop(name: str, prev: float, cur: float, store_code: str = "") -> str:
    store = f"（门店 {store_code}）" if store_code else ""
    return (f"{nick()}！{name} 从 {prev:g} 块降到 {cur:g} 块，"
            f"刷新历史最低！{store}要不要我把单备好？")


def on_coupon_usable(name: str, saving: float) -> str:
    return (f"{nick()}，你常点的 {name}，今天有张券预计优惠 {saving:g} 元，"
            f"到手价我给你算好了。")


def on_coupons_claimed(n: int) -> str:
    return f"{nick()}，今天 {n} 张券都帮你领进卡包了，不领白不领。"


def on_meal_time() -> str:
    return f"{nick()}，到饭点了！附近店有优惠，点一下就成。"


def on_savings_report(saved: float, drops: int, period: str = "这周",
                      potential: float = 0.0, unverified: int = 0) -> str:
    parts = [f"{period}订单详情已核实优惠 {saved:g} 元"]
    if potential > 0:
        parts.append(f"另有 {potential:g} 元潜在优惠（未购买，不计入实际省下）")
    if unverified > 0:
        parts.append(f"{unverified} 单优惠待订单详情核实")
    parts.append(f"发现降价 {drops} 次")
    return f"{nick()}，" + "；".join(parts) + "。"


def confirm_gate(draft: dict) -> str:
    """下单前确认话术（红线：只确认，不代点）。

    省 0 元时不硬塞「省 0」，避免尴尬。
    """
    coupon = draft.get("coupon")
    orig = draft.get("originalPrice", 0)
    price = draft.get("price", 0)
    discount = float(draft.get("discount") or 0)
    if coupon:
        body = f"原价 {orig:g}，用「{coupon.get('name') or coupon.get('couponCode')}」后 {price:g}"
    else:
        body = f"原价 {orig:g}，到手 {price:g}"
    if discount > 0:
        body += f"，预计优惠 {discount:g} 元（下单前报价）"
    return f"{nick()}，这单我算好了：{body}。你确认我就下，不确认我不动。"


def confirm_gate_scheduled(draft: dict) -> str:
    """到点确认话术：强调「按此刻重新算价」（券可能过期、价可能变了）。"""
    sched = draft.get("scheduledFrom") or {}
    at = sched.get("at") or "到点"
    coupon = draft.get("coupon")
    orig = draft.get("originalPrice", 0)
    price = draft.get("price", 0)
    discount = float(draft.get("discount") or 0)
    if coupon:
        coupon_txt = f'用券「{coupon.get("name") or coupon.get("couponCode")}」'
        if coupon.get("expireAt"):
            coupon_txt += f'（有效期至 {coupon["expireAt"]}）'
    else:
        coupon_txt = "这单没券可用"
    parts = [f"原价 {orig:g}", f"到手 {price:g}"]
    if discount > 0:
        parts.append(f"预计优惠 {discount:g} 元")
    return (f"{nick()}，{at} 的单我按此刻的价格重新算好了："
            + "，".join(parts) + f"，{coupon_txt}。你确认我就下，不确认我不动。")


def blocked_by_gate() -> str:
    return ("这单还没经过你确认，我不会动。看完草稿明细后回复「确认下单」"
            "我再执行。")
