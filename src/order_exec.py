# -*- coding: utf-8 -*-
"""G · 定时执行 + 人类确认闸

红线：create-order / mall-create-order / cancel-order 必须由账号持有者
本人显式确认。本模块提供 confirm_receipt（确认回执）机制：
CLI 的 order 命令必须带 --confirm 且用户在对话中明确同意过草稿内容。

确认闸（v0.23 强化）——「确认」必须绑定「刚展示的那份草稿」：
  ① 草稿自带 quotedAt（报价时间）与 digest（内容摘要），防止文件被篡改；
  ② 下单前必须**重新算价**并与草稿比对，价格/券/商品变了就拒绝；
  ③ 草稿报价超过 TTL 即失效、用券已过期即拒绝；
  ④ confirm-text 必须精确匹配一次性确认凭证（非任意非空字符串）。
"""
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from .config import CACHE_DIR
from .mcd_client import MONEY_WRITE

DRAFT_DIR = CACHE_DIR / "drafts"
RECEIPT_LOG = CACHE_DIR / "order_receipts.jsonl"

QUOTE_TTL_SECONDS = 30 * 60        # 草稿报价有效期：超过需重新算价
CONFIRM_TEXT = "确认下单"            # 一次性确认凭证：必须精确匹配


# ---------------------------------------------------------------------------
# 草稿完整性
# ---------------------------------------------------------------------------
def draft_digest(draft: dict) -> str:
    """草稿内容摘要（sha256 前 16 位）：覆盖会改变下单结果的核心字段。

    用于确认前校验「用户看过的那份草稿文件」是否被篡改。
    """
    core = {
        "storeCode": draft.get("storeCode"),
        "takeWayCode": draft.get("takeWayCode"),
        "items": [(it.get("productCode"), it.get("quantity"))
                  for it in (draft.get("items") or [])],
        "coupon": ((draft.get("coupon") or {}).get("couponId")
                   or (draft.get("coupon") or {}).get("couponCode") or None),
        "price": draft.get("price"),
        "originalPrice": draft.get("originalPrice"),
    }
    canon = json.dumps(core, ensure_ascii=False, sort_keys=True,
                       default=str, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def load_draft(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 确认闸
# ---------------------------------------------------------------------------
def human_gate(draft: dict) -> bool:
    """确认闸：返回 False 时绝不执行。

    draft 必须带 confirmedByHuman 标记 + 确认时间 + 精确匹配的确认文本。
    """
    if not draft.get("confirmedByHuman"):
        return False
    if not draft.get("confirmedAt"):
        return False
    if str(draft.get("confirmedText") or "").strip() != CONFIRM_TEXT:
        return False
    return True


def validate_for_order(draft: dict, now: datetime | None = None) -> tuple[bool, str]:
    """下单前校验草稿有效性（确认凭据之外的「内容/时效」维度）。

    返回 (是否放行, 拒绝原因)。确认凭据（confirmedByHuman/confirmedText）
    由 human_gate 负责，这里校验草稿内容是否仍可信：
      ① 内容摘要一致（防篡改）
      ② 报价未超 TTL（防隔夜旧草稿）
      ③ 用券未过期
    """
    now = now or datetime.now()

    if draft.get("digest") and draft_digest(draft) != draft["digest"]:
        return False, "草稿内容与生成时不一致（可能被改动），请重新执行 best 算价"

    quoted = draft.get("quotedAt")
    if quoted:
        try:
            qt = datetime.strptime(str(quoted)[:19], "%Y-%m-%d %H:%M:%S")
            age = (now - qt).total_seconds()
            if age > QUOTE_TTL_SECONDS:
                return False, ("草稿报价已过期（" + str(QUOTE_TTL_SECONDS // 60)
                               + " 分钟前算的价），请重新执行 best 算价")
        except ValueError:
            pass

    coupon = draft.get("coupon") or {}
    exp = coupon.get("expireAt")
    if exp:
        try:
            et = datetime.strptime(str(exp)[:19], "%Y-%m-%d %H:%M:%S")
            if now > et:
                return False, ("用券「" + str(coupon.get("name") or coupon.get("couponCode"))
                               + "」已过期，请重新执行 best 算价")
        except ValueError:
            pass

    return True, ""


def diff_draft(old: dict, new: dict) -> list[str]:
    """比对两份草稿的关键下单字段，返回变化描述列表（空 = 一致）。

    下单前把「用户看过的草稿」与「此刻重新算出的草稿」比对，
    任何会改变下单结果的变化都要列出，由调用方拒绝并重新展示。
    """
    changes: list[str] = []

    def cmp(key, label, fmt=None):
        a, b = old.get(key), new.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if round(float(a), 2) != round(float(b), 2):
                changes.append(f"{label}: {fmt(a) if fmt else a} → {fmt(b) if fmt else b}")
        elif a != b:
            changes.append(f"{label}: {fmt(a) if fmt else a} → {fmt(b) if fmt else b}")

    cmp("price", "到手价")
    cmp("originalPrice", "原价")

    oc, nc = old.get("coupon") or {}, new.get("coupon") or {}
    oc_id = oc.get("couponId") or oc.get("couponCode")
    nc_id = nc.get("couponId") or nc.get("couponCode")
    if oc_id != nc_id:
        changes.append(f"用券: {oc.get('name') or '无'} → {nc.get('name') or '无'}")

    oi = [(i.get("productCode"), i.get("quantity")) for i in (old.get("items") or [])]
    ni = [(i.get("productCode"), i.get("quantity")) for i in (new.get("items") or [])]
    if oi != ni:
        changes.append("商品清单已变化")

    return changes


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
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
    """落盘草稿：补报价时间 quotedAt + 内容摘要 digest（供确认闸校验）。"""
    if not draft.get("quotedAt"):
        draft["quotedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    draft["digest"] = draft_digest(draft)
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    p = DRAFT_DIR / f"draft-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)
    return p


def _log_receipt(receipt: dict):
    RECEIPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RECEIPT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, ensure_ascii=False) + "\n")
