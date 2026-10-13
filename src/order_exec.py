# -*- coding: utf-8 -*-
"""G · 定时执行 + 人类确认闸

红线：create-order / mall-create-order / cancel-order 必须由账号持有者
本人显式确认。本模块提供 confirm_receipt（确认回执）机制：
CLI 的 order 命令必须带 --confirm 且用户在对话中明确同意过草稿内容。
"""
import json
import time
from pathlib import Path

from .config import CACHE_DIR
from .mcd_client import MONEY_WRITE

DRAFT_DIR = CACHE_DIR / "drafts"
RECEIPT_LOG = CACHE_DIR / "order_receipts.jsonl"


def load_draft(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def human_gate(draft: dict) -> bool:
    """确认闸展示逻辑：返回 False 时绝不执行。

    draft 必须带 confirmedByHuman 标记（由 CLI 在用户明确同意后写入）。
    """
    if not draft.get("confirmedByHuman"):
        return False
    if not draft.get("confirmedAt") or not draft.get("confirmedText"):
        return False
    return True


def execute_draft(client, draft: dict) -> dict:
    """执行下单（前提：已过 human_gate）。返回回执并落盘审计日志。"""
    if not human_gate(draft):
        raise PermissionError(
            "红线拦截：草稿未经人类确认（confirmedByHuman=false）。"
            "请向用户展示草稿明细，等其明确回复确认后再执行。"
        )
    args = {
        "storeCode": draft["storeCode"],
        "takeWayCode": draft["takeWayCode"],
        "items": draft["items"],
    }
    if draft.get("coupon"):
        args["couponId"] = draft["coupon"]["couponId"]
        args["couponCode"] = draft["coupon"]["couponCode"]
    if draft.get("reservationDate"):
        args["reservationDate"] = draft["reservationDate"]

    result = client.call("create-order", args, confirm=True)
    receipt = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "confirmedText": draft.get("confirmedText"),
        "storeCode": draft["storeCode"],
        "price": draft.get("price"),
        "result": result,
    }
    _log_receipt(receipt)
    # 真实优惠入账本（精确值：原价-实付）
    try:
        from . import ledger
        ledger.append_event(
            "order", storeCode=draft.get("storeCode"),
            name=",".join(str(it.get("name") or it.get("productCode"))
                          for it in draft.get("items", [])),
            originalPrice=draft.get("originalPrice"), price=draft.get("price"),
            saved=round(float(draft.get("discount") or 0), 2),
            takeWay=draft.get("takeWayCode"),
        )
    except Exception:
        pass  # 账本失败不影响下单结果
    return receipt


def save_draft(draft: dict) -> Path:
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    p = DRAFT_DIR / f"draft-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)
    return p


def _log_receipt(receipt: dict):
    RECEIPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RECEIPT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, ensure_ascii=False) + "\n")
