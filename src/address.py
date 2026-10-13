# -*- coding: utf-8 -*-
"""常用地址自动推导（省掉手填）

数据来源与优先级：
  ① delivery-query-addresses —— 用户自己在外卖里保存的地址，**权威来源**，
     直接可喂给 query-nearby-stores（MCP 只吃地址文本，不读 GPS）。
  ② 订单里的 storeAddress —— 只能推「门店地址」，不能推用户家地址；
     但可作为「查周边门店」的兜底锚点（人在附近时依然能定位到店）。

隐私红线：
  - 只落 fullAddress；手机号/联系人一律不写盘（接口本身返回的也是掩码号）。
  - 写入仅限 config.local.json（已被 .gitignore 排除），且必须调用方显式 apply。
"""
from __future__ import annotations

import json
import re

# 行政区关键词，用于判断「哪条地址与常去门店同片」
_DISTRICTS = ("区", "县", "市")


def derive_addresses(client) -> dict:
    """只读推导地址。返回

    {
      "primary": "..." | "",          # 喂给 query-nearby-stores 的主地址
      "candidates": [{"fullAddress","addressId","source"}...],
      "storeAnchor": {"storeCode","storeName","storeAddress"} | None,
      "notes": [提示...],
    }
    """
    out: dict = {"primary": "", "candidates": [], "storeAnchor": None, "notes": []}

    # ① 外卖地址（权威）
    try:
        raw = client.call("delivery-query-addresses")
        d = raw.get("data") if isinstance(raw, dict) else raw
        addrs = (d or {}).get("addresses") or [] if isinstance(d, dict) else []
        for a in addrs:
            fa = (a.get("fullAddress") or "").strip()
            if fa:
                out["candidates"].append({
                    "fullAddress": fa,
                    "addressId": a.get("addressId"),
                    "source": "delivery-query-addresses",
                })
        if not out["candidates"]:
            out["notes"].append("外卖地址列表为空（可能从未在麦乐送保存过地址）")
    except Exception as e:
        out["notes"].append(f"delivery-query-addresses 失败: {e}")

    # ② 订单门店锚点（兜底）
    try:
        orders = client.order_list()
        d = orders.get("data") if isinstance(orders, dict) else orders
        lst = d if isinstance(d, list) else (d or {}).get("list") or (d or {}).get("orders") or []
        for od in lst:
            sc = od.get("storeCode")
            if not sc:
                continue
            sa = od.get("storeAddress")
            if not sa:  # order-list 不带门店地址时才查详情
                try:
                    det = client.query_order(str(od.get("orderId") or od.get("orderNo")))
                    dd = det.get("data") if isinstance(det, dict) else det
                    if isinstance(dd, dict):
                        sa = dd.get("storeAddress")
                except Exception:
                    sa = None
            if sa:
                out["storeAnchor"] = {
                    "storeCode": sc, "storeName": od.get("storeName") or "",
                    "storeAddress": sa,
                }
                break
    except Exception as e:
        out["notes"].append(f"order-list 失败: {e}")

    # ③ 选主地址：与常去门店同片（同关键词/同行政区）者优先
    out["primary"] = _pick_primary(out["candidates"], out["storeAnchor"])
    return out


def _pick_primary(candidates: list[dict], anchor: dict | None) -> str:
    if not candidates:
        return ""
    if not anchor:
        return candidates[0]["fullAddress"]

    anchor_addr = (anchor or {}).get("storeAddress") or ""
    anchor_name = (anchor or {}).get("storeName") or ""
    tokens = _addr_tokens(anchor_addr) | _addr_tokens(anchor_name)

    best, best_score = candidates[0]["fullAddress"], -1
    for c in candidates:
        toks = _addr_tokens(c["fullAddress"])
        score = len(tokens & toks)
        # 命中门店所在大厦/小区名权重更高
        if tokens and toks and max(len(tokens & toks), 0):
            score += 1
        if score > best_score:
            best, best_score = c["fullAddress"], score
    return best


def _addr_tokens(s: str) -> set[str]:
    """抽取地址里的可比对片段：行政区 + 3~8 字建筑/小区名候选。"""
    s = (s or "").strip()
    if not s:
        return set()
    toks = set(re.findall(r"[\u4e00-\u9fa5]{2,12}", s))
    # 去掉通用词噪声
    noise = {"街道", "社区", "一层", "铺位", "办公楼", "餐厅", "麦当劳", "中国", "广东省"}
    return {t for t in toks if t not in noise and len(t) >= 2}


def describe(derived: dict) -> str:
    """给人看的推导结果（地址属于个人信息，只在本地终端展示）。"""
    lines = ["【常用地址推导结果】"]
    if derived.get("primary"):
        lines.append(f"主地址（查店用）: {derived['primary']}")
    else:
        lines.append("主地址: 未推导到")
    for i, c in enumerate(derived.get("candidates") or [], 1):
        lines.append(f"  候选{i}: {c['fullAddress']}　← {c['source']}")
    a = derived.get("storeAnchor")
    if a:
        lines.append(f"门店锚点: {a.get('storeName')}（{a.get('storeCode')}）{a.get('storeAddress')}")
        lines.append("  ↑ 订单只能推门店地址，推不出你家门牌；但可当查周边门店的兜底锚点")
    for n in derived.get("notes") or []:
        lines.append(f"  提示: {n}")
    return "\n".join(lines)


def to_config_patch(derived: dict) -> dict:
    """转成可写入 config.local.json 的 user 段补丁（不含手机号/联系人）。"""
    patch = {}
    if derived.get("primary"):
        patch["default_address"] = derived["primary"]
        patch["delivery_address"] = derived["primary"]
    return patch


def dumps(patch: dict) -> str:
    return json.dumps(patch, ensure_ascii=False, indent=2)