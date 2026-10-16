# -*- coding: utf-8 -*-
"""取消未支付订单（MONEY_WRITE，必须人类确认闸）

红线：cancel-order 属于花钱/写操作，客户端层需 confirm=True；
CLI 层再叠加 --yes + --confirm-text「确认取消」双重确认。

「未支付」识别策略（保守，宁漏勿误）：
  - order-list 只返回 orderStatus 中文文本（当前实测 10 单全为「订单已完成」，
    无未支付样本可验证），故只按明确关键词命中「未支付/待支付」，且排除终态。
  - 未知状态一律**不**判为未支付（避免误取消已支付订单）。

⚠️ 扫描范围：order-list 无分页参数、只返回近期订单（约最近 10 笔），
本命令只能覆盖「近期可见」范围的未支付单，更早的未支付单不在清理范围内。
"""
from __future__ import annotations

from .mcd_client import McdClient

CONFIRM_TEXT = "确认取消"          # 一次性确认凭据：必须精确匹配
DEFAULT_REASON = "-1"             # 取消原因：-1=其它原因（批量清理未支付单的通用语义）

# 明确「未支付」的状态文本关键词（命中即视为可取消）
_UNPAID_HINTS = ("待支付", "未支付", "待付款", "等待付款", "未付款", "待付款")

# 明确「已完结/不可取消」的终态关键词（优先排除）
_FINAL_HINTS = ("已完成", "已取消", "已关闭", "已退款", "已核销", "已失效", "退款中", "完成")


def is_unpaid_status(status: str | None) -> bool:
    """判断订单状态文本是否代表「未支付」。

    保守逻辑：终态 → False；命中未支付关键词 → True；其余未知状态 → False。
    """
    status = (status or "").strip()
    if not status:
        return False
    if any(k in status for k in _FINAL_HINTS):
        return False
    return any(k in status for k in _UNPAID_HINTS)


def list_unpaid_orders(client: McdClient) -> tuple[list[dict], list[dict]]:
    """拉取订单列表，筛出「未支付」订单。

    返回 (未支付列表, 全量列表)。全量列表供预览展示「共扫了多少单」。
    """
    raw = client.order_list()
    d = raw.get("data") if isinstance(raw, dict) else raw
    if isinstance(d, dict):
        items = d.get("list") or []
    elif isinstance(d, list):
        items = d
    else:
        items = []
    unpaid = [o for o in items if is_unpaid_status(o.get("orderStatus"))]
    return unpaid, items


def cancel_orders(client: McdClient, orders: list[dict], reason: str = DEFAULT_REASON) -> dict:
    """逐个取消订单，返回回执 {cancelled[], failed[]}。

    每单独立调用、独立捕获异常：失败不中断整批，逐单留痕。
    """
    receipt = {"cancelled": [], "failed": [], "reason": reason}
    for o in orders:
        oid = o.get("orderId")
        if not oid:
            receipt["failed"].append({"orderId": None, "error": "订单缺少 orderId"})
            continue
        try:
            result = client.call(
                "cancel-order",
                {"orderId": oid, "cancelReasonCode": str(reason)},
                confirm=True,
            )
            ledger_recorded = False
            try:
                from . import ledger
                ledger_recorded = ledger.record_cancellation(oid) is not None
            except Exception:
                pass  # 账本失败不影响取消结果
            receipt["cancelled"].append({"orderId": oid, "result": result,
                                         "ledgerRecorded": ledger_recorded})
        except Exception as e:
            receipt["failed"].append({"orderId": oid, "error": str(e)})
    return receipt
