# -*- coding: utf-8 -*-
"""人设话术层：「老大」人格

三原则：① 干脆有汇报感 ② 省了多少讲成数字 ③ 到点有画面感。
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
    return (f"{nick()}，你常点的 {name}，今天有张券能再省 {saving:g} 块，"
            f"到手价我给你算好了。")


def on_coupons_claimed(n: int) -> str:
    return f"{nick()}，今天 {n} 张券都帮你领进卡包了，不领白不领。"


def on_meal_time() -> str:
    return f"{nick()}，到饭点了！附近店有优惠，点一下就成。"


def on_savings_report(saved: float, drops: int, period: str = "这周") -> str:
    return (f"{nick()}，{period}帮你省了 {saved:g}，抓了 {drops} 次降价，"
            f"战绩能发朋友圈了。")


def confirm_gate(draft: dict) -> str:
    """下单前确认话术（红线：只确认，不代点）。"""
    coupon = draft.get("coupon")
    coupon_txt = f"用「{coupon.get('name') or coupon.get('couponCode')}」" if coupon else "未用券"
    return (
        f"{nick()}，这单我算好了：原价 {draft.get('originalPrice', 0):g}，"
        f"{coupon_txt}后 {draft.get('price', 0):g}，省 {draft.get('discount', 0):g}。"
        f"你确认我就下，不确认我不动。"
    )


def blocked_by_gate() -> str:
    return ("这单还没经过你确认，我不会动。看完草稿明细后回复「确认下单」"
            "我再执行。")
