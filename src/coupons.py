# -*- coding: utf-8 -*-
"""C · 自动领券（免费写操作，允许自动执行，留回执）"""
import time


def auto_claim(client) -> dict:
    """available-coupons → auto-bind-coupons → query-my-coupons 校验入库。

    返回回执 {claimed, walletCount, ts}。失败不无限重试。
    """
    receipt = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "available": [], "bindResult": None, "walletCount": None,
               "errors": []}

    try:
        avail = client.available_coupons()
        d = avail.get("data") if isinstance(avail, dict) else avail
        items = d if isinstance(d, list) else (d or {}).get("list", []) if isinstance(d, dict) else []
        # 实测：{couponName, couponStatus, label}；未领的才值得领
        receipt["available"] = [
            {"name": c.get("couponName") or c.get("name") or c.get("title"),
             "status": c.get("couponStatus") or "", "label": c.get("label") or ""}
            for c in items
        ]
    except Exception as e:
        receipt["errors"].append(f"available-coupons: {e}")

    try:
        bind = client.call("auto-bind-coupons")
        receipt["bindResult"] = bind
    except Exception as e:
        receipt["errors"].append(f"auto-bind-coupons: {e}")

    # 校验入库（实测：query-my-coupons → data.coupons[]）
    try:
        mine = client.my_coupons()
        d = mine.get("data") if isinstance(mine, dict) else mine
        items: list = []
        if isinstance(d, list):
            items = d
        elif isinstance(d, dict):
            for k in ("coupons", "list", "couponList"):
                if isinstance(d.get(k), list):
                    items = d[k]
                    break
        receipt["walletCount"] = len(items)
    except Exception as e:
        receipt["errors"].append(f"query-my-coupons: {e}")

    return receipt
