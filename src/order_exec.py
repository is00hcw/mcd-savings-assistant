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
from .mcd_client import MONEY_WRITE, NetworkError

DRAFT_DIR = CACHE_DIR / "drafts"
RECEIPT_LOG = CACHE_DIR / "order_receipts.jsonl"

QUOTE_TTL_SECONDS = 30 * 60        # 草稿报价有效期：超过需重新算价
CONFIRM_TEXT = "确认下单"            # 一次性确认凭证：必须精确匹配


# ---------------------------------------------------------------------------
# 草稿完整性
# ---------------------------------------------------------------------------
def _items_fingerprint(items) -> list[str]:
    """把商品清单规范化为可比较的指纹（覆盖完整业务字段）。

    execute_draft 会把 draft['items'] **原样**传给 create-order，因此每个 item 的
    全部字段（productCode / quantity / 套餐规格 / 替换项 / roundList 等）都可能
    影响最终订单结果，摘要与 diff 必须覆盖完整 item 内容，不能只挑
    productCode + quantity——否则保持这两项不变、只改 roundList 就能绕过篡改检测。
    """
    return [json.dumps(it, ensure_ascii=False, sort_keys=True, default=str,
                       separators=(",", ":"))
            for it in (items or [])]


def draft_digest(draft: dict) -> str:
    """草稿内容摘要（sha256 前 16 位）：覆盖 execute_draft 实际发给 create-order
    的全部字段。

    用于确认前校验「用户看过的那份草稿文件」是否被篡改。摘要必须覆盖**下单请求
    用到的每一个字段**——beType / reservationDate / couponId / couponCode 都是
    execute_draft 原样透传给 create-order 的，只摘要其中一部分就等于留了篡改
    漏洞（改没被摘要的那一项，digest 不变却能改下单结果）。
    """
    coupon = draft.get("coupon") or {}
    core = {
        "storeCode": draft.get("storeCode"),
        "takeWayCode": draft.get("takeWayCode"),
        "orderType": draft.get("orderType", 1),
        "beType": draft.get("beType", 1),
        "beCode": draft.get("beCode"),
        "addressId": draft.get("addressId"),
        "reservationDate": draft.get("reservationDate"),
        "items": _items_fingerprint(draft.get("items")),
        "couponId": coupon.get("couponId"),
        "couponCode": coupon.get("couponCode"),
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

    # ① 内容摘要（digest）**强制校验**：缺 digest 或对不上都拒绝。
    #    只在校验「存在时」会留下漏洞——删掉 digest 字段即可绕过篡改检测。
    digest = draft.get("digest")
    if not digest:
        return False, ("草稿缺少内容摘要（digest），可能是旧版或手工构造的草稿，"
                       "请重新执行 best 算价")
    if draft_digest(draft) != digest:
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

    cmp("storeCode", "门店")
    cmp("takeWayCode", "取餐方式")
    cmp("orderType", "订单类型")
    cmp("beType", "业务类型")
    cmp("beCode", "业务编码")
    cmp("addressId", "配送地址")
    cmp("deliveryFee", "配送费")
    cmp("price", "到手价")
    cmp("originalPrice", "原价")
    cmp("reservationDate", "预约时间")

    oc, nc = old.get("coupon") or {}, new.get("coupon") or {}
    # couponId 与 couponCode 都会透传给 create-order，任一变化都要捕捉
    if (oc.get("couponId") != nc.get("couponId")
            or oc.get("couponCode") != nc.get("couponCode")):
        changes.append(f"用券: {oc.get('name') or '无'} → {nc.get('name') or '无'}")

    if _items_fingerprint(old.get("items")) != _items_fingerprint(new.get("items")):
        changes.append("商品清单已变化")

    return changes


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def _order_id_snapshot(client) -> set[str] | None:
    """下单前记录现有订单 id 快照，用于「结果不确定」时判断是否已创建新单。

    查询失败返回 None（无法做差异判断，调用方按「无法确认」处理）。
    """
    try:
        raw = client.order_list()
    except Exception:
        return None
    d = raw.get("data") if isinstance(raw, dict) else raw
    items = d.get("list") if isinstance(d, dict) else (d if isinstance(d, list) else [])
    return {str(o.get("orderId")) for o in items if o.get("orderId")}


def _uncertain_receipt(client, draft: dict, err: Exception,
                       before_ids: set[str] | None) -> dict:
    """下单遭遇网络层「结果不确定」：查询订单状态判断是否已创建，绝不重试。

    返回 status="uncertain" 的回执（仍落审计日志），由上层明确提示用户
    到 App 核对，避免重复下单。
    """
    from .cancel import is_unpaid_status

    probe = {"queried": False, "newUnpaidOrder": None, "note": ""}
    if before_ids is not None:
        try:
            raw = client.order_list()
            d = raw.get("data") if isinstance(raw, dict) else raw
            items = d.get("list") if isinstance(d, dict) else (d if isinstance(d, list) else [])
            new_unpaid = [o for o in items
                          if str(o.get("orderId")) not in before_ids
                          and is_unpaid_status(o.get("orderStatus"))]
            probe["queried"] = True
            probe["newUnpaidOrder"] = bool(new_unpaid)
            probe["note"] = (
                "检测到新增未支付订单，下单很可能已成功"
                if new_unpaid else "未检测到新增未支付订单，下单可能未成功"
            )
        except Exception as pe:
            probe["note"] = f"订单状态查询失败: {pe}"
    receipt = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "uncertain",
        "confirmedText": draft.get("confirmedText"),
        "storeCode": draft.get("storeCode"),
        "price": draft.get("price"),
        "error": str(err),
        "probe": probe,
        "warning": (
            "下单结果不确定（网络层错误），已停止自动重试以避免重复下单。"
            "请到麦当劳 App「我的订单」确认是否已生成该订单："
            "若已生成则无需再操作；若没有，再重新执行下单。"
        ),
    }
    _log_receipt(receipt)
    return receipt


def execute_draft(client, draft: dict) -> dict:
    """执行下单（前提：已过 human_gate）。返回回执并落盘审计日志。

    网络层「结果不确定」时不盲目重试：记录下单前订单快照，出错后查订单状态，
    返回 status="uncertain" 回执（不会重复提交）。
    """
    if not human_gate(draft):
        raise PermissionError(
            "红线拦截：草稿未经人类确认（confirmedByHuman=false）。"
            "请向用户展示草稿明细，等其明确回复确认后再执行。"
        )
    order_type = draft.get("orderType", 1)
    be_type = draft.get("beType", 1)
    args = {
        "storeCode": draft["storeCode"],
        "orderType": order_type,
        "beType": be_type,
        "items": draft["items"],
    }
    if order_type == 2:  # 外卖：必传 beCode + addressId，不传 takeWayCode
        args["beCode"] = draft.get("beCode")
        args["addressId"] = draft.get("addressId")
    else:  # 到店/得来速：传 takeWayCode
        args["takeWayCode"] = draft["takeWayCode"]
    if draft.get("coupon"):
        args["couponId"] = draft["coupon"]["couponId"]
        args["couponCode"] = draft["coupon"]["couponCode"]
    if draft.get("reservationDate"):
        args["reservationDate"] = draft["reservationDate"]

    before_ids = _order_id_snapshot(client)
    try:
        result = client.call("create-order", args, confirm=True)
    except NetworkError as e:
        return _uncertain_receipt(client, draft, e, before_ids)

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
            takeWay=draft.get("takeWay") or draft.get("takeWayCode"),
        )
    except Exception:
        pass  # 账本失败不影响下单结果
    return receipt


def save_draft(draft: dict) -> Path:
    """落盘草稿：补报价时间 quotedAt + 内容摘要 digest（供确认闸校验）。

    草稿 ID 用「时间戳(微秒) + digest 前 8 位」双重保证：微秒时间戳让同秒生成的
    多份草稿文件名互不相同（不再同秒覆盖）；digest 前缀兜底，即使同一微秒内容
    不同也必不同名，杜绝「确认 A 草稿却拿到 B 草稿文件」的错配。
    """
    if not draft.get("quotedAt"):
        draft["quotedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    draft["digest"] = draft_digest(draft)
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    p = DRAFT_DIR / f"draft-{ts}-{draft['digest'][:8]}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)
    return p


def _log_receipt(receipt: dict):
    RECEIPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(RECEIPT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(receipt, ensure_ascii=False) + "\n")
