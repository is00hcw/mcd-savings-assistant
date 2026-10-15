# -*- coding: utf-8 -*-
"""C · 自动领券（免费写操作，允许自动执行，留回执）"""
from __future__ import annotations

import time

# 已领取状态取值（实测 couponStatus 可能是英文枚举，label 可能含「已领取」）
_CLAIMED_STATUSES = ("HAVING_RECEIVE", "RECEIVED", "ALREADY_RECEIVE", "已领取")


def is_claimable(coupon: dict) -> bool:
    """判断一张券是否仍可领取（未领取）。

    双保险：couponStatus 字段 + label 文本。不再只靠 label 里的「已领取」
    字符串——接口若改用状态字段表示已领取，也能正确排除。
    """
    if not isinstance(coupon, dict):
        return False
    # 没有任何券标识 → 不是有效券，不算「可领」
    ident = (coupon.get("couponName") or coupon.get("name") or coupon.get("title")
             or coupon.get("id") or coupon.get("couponId")
             or coupon.get("couponCode") or coupon.get("code"))
    if not ident:
        return False
    status = str(coupon.get("couponStatus") or coupon.get("status") or "").strip()
    if status.upper() in _CLAIMED_STATUSES or status == "已领取":
        return False
    label = str(coupon.get("label") or "")
    if "已领取" in label:
        return False
    return True


def _wallet_ids(client, errors: list) -> set[str] | None:
    """query-my-coupons 的券 id 集合（用于领前/领后差异核验）。

    查询失败返回 **None**（区别于「卡包为空」的空集合）——调用方不能把
    None 当空集合做差集，否则领前失败/领后成功会把整个卡包误报成「本次领到」。
    """
    try:
        mine = client.my_coupons()
    except Exception as e:
        errors.append(f"query-my-coupons: {e}")
        return None
    d = mine.get("data") if isinstance(mine, dict) else mine
    items: list = []
    if isinstance(d, list):
        items = d
    elif isinstance(d, dict):
        for k in ("coupons", "list", "couponList"):
            if isinstance(d.get(k), list):
                items = d[k]
                break
    ids: set[str] = set()
    for c in items:
        cid = str(c.get("id") or c.get("couponId") or c.get("code")
                  or c.get("couponCode") or "")
        if cid:
            ids.add(cid)
    return ids


def auto_claim(client) -> dict:
    """available-coupons → auto-bind-coupons → query-my-coupons 前后差异核验。

    返回回执 {available, bindResult, walletCount, claimedCount, claimedIds, errors}。
    「本次领到」不再按候选列表数量推算，而是领前/领后卡包券 id 集合的差集，
    避免把「本来就已领的券」误算成本次领到。失败不无限重试。
    """
    receipt = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "available": [], "bindResult": None,
               "walletCount": None, "claimedCount": 0, "claimedIds": [],
               "errors": []}

    # ① 领前卡包快照（差异核验基线）
    before = _wallet_ids(client, receipt["errors"])

    # ② 可领券清单
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

    # ③ 一键领取
    try:
        bind = client.call("auto-bind-coupons")
        receipt["bindResult"] = bind
    except Exception as e:
        receipt["errors"].append(f"auto-bind-coupons: {e}")

    # ④ 领后卡包快照 → 差集 = 本次真正领到。
    #    领前或领后任一次核验失败 → 数量不可信，宁报「未知」也不虚报
    #    （领前失败+领后成功时，after-before 会把整个已有卡包误算成本次领到）。
    after = _wallet_ids(client, receipt["errors"])
    receipt["walletCount"] = len(after) if after is not None else None
    if before is None or after is None:
        receipt["claimedIds"] = []
        receipt["claimedCount"] = None
        receipt["errors"].append("卡包核验不完整，「本次领到」数量未知，请到 App 核对")
    else:
        receipt["claimedIds"] = sorted(after - before)
        receipt["claimedCount"] = len(receipt["claimedIds"])

    return receipt
