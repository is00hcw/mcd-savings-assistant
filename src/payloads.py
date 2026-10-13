# -*- coding: utf-8 -*-
"""业务对象 → 结构化 payload（14.3 原则①：核心逻辑只产出结构化数据）

渲染层（内联卡片 / HTML / 图片）一律不写在这里，由 present.render 分发。
"""
from . import present, persona, order_exec, coupons


# ---------------------------------------------------------------------------
# alert：降价提醒 / 领券回执 / 到点提醒（内联卡片）
# ---------------------------------------------------------------------------
def alert_price_drop(events: list[dict], nick: str = "老大",
                     scanned: int = 0) -> dict:
    """盯价巡检的降价/新低 → alert payload。"""
    new_lows = [e for e in events if e.get("type") in ("new_low", "dropped")]
    if not new_lows:
        return present.make_payload(
            "alert", "暂无新优惠", f"{nick}，这轮巡检没抓到降价，我继续盯着。",
            metrics=[{"label": "巡检门店", "value": scanned, "unit": "家"}],
        )
    items = []
    metrics = [{"label": "抓到降价", "value": len(new_lows), "unit": "个", "emphasis": True}]
    metrics.append({"label": "巡检门店", "value": scanned, "unit": "家"})
    saved = 0.0
    for e in new_lows[:8]:
        cur = float(e.get("currentPrice") or 0)
        prev = float(e.get("prevMin") or e.get("prev") or 0)
        orig = float(e.get("originalPrice") or 0)
        if prev and cur:
            saved += max(prev - cur, 0)
        items.append({
            "name": e.get("name"), "storeCode": e.get("storeCode") or "",
            "price": cur,
            "note": f'历史最低 {prev:g}' if prev else (f'原价 {orig:g}' if orig else ""),
        })
    metrics.insert(0, {"label": "按历史最低省", "value": round(saved, 2), "unit": "元", "emphasis": True})
    return present.make_payload(
        "alert", "降价提醒", persona.on_price_drop(
            items[0]["name"],
            float(new_lows[0].get("prevMin") or new_lows[0].get("prev") or 0),
            float(new_lows[0].get("currentPrice") or 0),
            new_lows[0].get("storeCode", "")),
        metrics=metrics, items=items,
        meta={"hint": "要下单？跑 best 生成草稿，确认后才会真下单。"},
    )


def alert_expiring_points(ep: dict, nick: str = "老大") -> dict | None:
    ep = ep or {}
    cur = float(ep.get("currentMonth") or 0)
    if cur <= 0:
        return None
    return present.make_payload(
        "alert", "积分要过期了",
        f"{nick}，本月将有 {cur:g} 分过期，余额 {ep.get('available', 0)}，别浪费。",
        metrics=[
            {"label": "本月将过期", "value": cur, "unit": "分", "emphasis": True},
            {"label": "可用余额", "value": ep.get("available"), "unit": "分"},
        ],
        meta={"hint": "积分商城兑换属花钱动作，需你本人确认才会执行。"},
    )


def alert_coupon_receipt(receipt: dict, nick: str = "老大") -> dict:
    """领券回执 → alert payload（券是免费动作，回执透明化）。

    「本次领到」用领前/领后卡包券 id 差集（claimedCount），
    而非按候选列表数量推算，避免把「本就已领的券」误算成本次领到。
    """
    avail = receipt.get("available") or []
    claimable = [c for c in avail if coupons.is_claimable(c)]
    n = int(receipt.get("claimedCount") or 0)
    wallet = receipt.get("walletCount")
    summary = (persona.on_coupons_claimed(n) if not receipt.get("errors")
               else f"{nick}，领券过程有报错，详见明细。")
    metrics = [{"label": "本次领到", "value": n, "unit": "张", "emphasis": True}]
    if wallet is not None:
        metrics.append({"label": "卡包券数", "value": wallet, "unit": "张"})
    items = [{"name": c.get("name"), "note": c.get("label") or c.get("status") or ""}
             for c in claimable[:8]]
    return present.make_payload(
        "alert", "领券回执", summary, metrics=metrics, items=items,
        meta={"hint": "；".join(receipt["errors"]) if receipt.get("errors") else "免费动作，已留回执。"},
    )


def alert_meal_time(nearby: list[dict] | None = None, nick: str = "老大",
                    items: list[dict] | None = None, sch: dict | None = None) -> dict:
    """到点提醒。若登记了商品/备注，则直接列出用户说好的那一单。"""
    items = list(items or [])
    if not items:
        # 没登记商品时退化为「附近可点的门店」清单
        items = [{"name": s.get("storeName"), "note": s.get("address", "")}
                 for s in (nearby or [])[:5]]
    metrics = [{"label": "到点", "value": (sch or {}).get("at") or "现在", "unit": "",
                "emphasis": True}]
    if sch and sch.get("store"):
        metrics.append({"label": "门店", "value": sch["store"], "unit": ""})
    title = "到点提醒" + (f'：{sch["note"]}' if sch and sch.get("note") else "")
    return present.make_payload(
        "alert", title,
        persona.on_meal_time() if not sch else
        f'{nick}，{sch.get("at")} 了，{sch.get("note") or "该吃点东西了"}。',
        metrics=metrics, items=items,
        meta={"hint": "到点提醒只是通知，不会自动下单；要下单请说「确认下单」。"},
    )


# ---------------------------------------------------------------------------
# confirm：下单确认闸（交互卡片，必须清晰可点、防误触）
# ---------------------------------------------------------------------------
def confirm_from_draft(draft: dict, draft_path: str = "") -> dict:
    """下单确认卡：逐件列出商品（名称 × 数量 · 单价 · 小计）+ 券 + 门店 + 取餐方式。"""
    coupon = draft.get("coupon")
    saved = float(draft.get("discount") or 0)
    items = []
    for it in draft.get("items", []):
        line = {
            "name": it.get("name") or it.get("productCode"),
            "quantity": it.get("quantity"),
            "price": it.get("subtotal") if it.get("subtotal") is not None else None,
            "note": _item_note(it),
        }
        items.append(line)

    store_line = f'{draft.get("storeName") or draft.get("storeCode")}'
    if draft.get("takeWayName"):
        store_line += f'（{draft["takeWayName"]}）'
    if coupon:
        coupon_txt = coupon.get("name") or coupon.get("couponCode")
        if coupon.get("expireAt"):
            coupon_txt += f'（有效期至 {coupon["expireAt"]}）'
    else:
        coupon_txt = "未用券/积分"

    metrics = [
        {"label": "原价", "value": draft.get("originalPrice"), "unit": "元"},
        {"label": "到手价", "value": draft.get("price"), "unit": "元", "emphasis": True},
    ]
    if saved > 0:
        metrics.append({"label": "省", "value": saved, "unit": "元", "emphasis": True})

    sched = draft.get("scheduledFrom")
    if sched:
        metrics.insert(0, {"label": "到点时间", "value": sched.get("at") or "到点即取",
                           "unit": "", "emphasis": True})
    if draft.get("quotedAt"):
        metrics.append({"label": "报价时间", "value": str(draft["quotedAt"])[11:16],
                        "unit": ""})

    # 到点场景与普通算价场景共用本函数：标题 / 话术 / 红线声明在此统一生成，
    # 避免 schedule.fire 再手工拼一份（文案漂移风险）。
    if sched:
        title = f'到点了：{sched.get("note") or "该下单了"}'
        summary = persona.confirm_gate_scheduled(draft)
        hint_parts = [f'到点任务 {sched.get("id") or ""}']
        if sched.get("note"):
            hint_parts.append(str(sched["note"]))
        hint_parts.append(store_line)
        hint_parts.append(f'券：{coupon_txt}')
        hint_parts.append("⛔ 这条只是确认消息，我不会自动下单。")
    else:
        title = "下单确认"
        summary = persona.confirm_gate(draft)
        hint_parts = [store_line, f'券：{coupon_txt}',
                      "未收到你的明确确认前，我不会调用 create-order。"]

    # 报价时效 + 结果完整性提示（统一追加，所有确认卡生效）
    if draft.get("quotedAt"):
        hint_parts.append(f'报价 {str(draft["quotedAt"])[11:16]}，'
                          f'{order_exec.QUOTE_TTL_SECONDS // 60} 分钟内有效，超时需重新确认')
    if draft.get("pricingWarnings"):
        hint_parts.append("⚠️ 结果可能不完整：" + "；".join(draft["pricingWarnings"]))

    return present.make_payload(
        "confirm", title, summary,
        metrics=metrics,
        items=items,
        actions=[
            {"id": "confirm", "label": "确认下单", "style": "primary",
             "command": f'python main.py order --draft "{draft_path}" --yes --confirm-text "确认下单"'},
            {"id": "cancel", "label": "再想想", "style": "ghost"},
        ],
        draft=draft,
        meta={
            "hint": "｜".join(hint_parts),
            "trace": draft.get("savingDetail", ""),
        },
    )


def _item_note(it: dict) -> str:
    """商品行补充：官方规格名（与用户原话不同时）· 单价 · 原价小计。"""
    parts = []
    if it.get("officialName") and it["officialName"] != it.get("name"):
        parts.append(str(it["officialName"]))
    if it.get("unitPrice") is not None:
        parts.append(f'单价 ¥{it["unitPrice"]:g}')
    if it.get("originalSubtotal") is not None and it.get("subtotal") is not None:
        if float(it["originalSubtotal"]) > float(it["subtotal"]):
            parts.append(f'原价 ¥{it["originalSubtotal"]:g}')
    if not parts and it.get("productCode"):
        parts.append(str(it["productCode"]))
    return " · ".join(parts)


def confirm_candidates(track_a: dict, track_b: dict) -> dict:
    """双轨候选（全局最低 vs 常买Top3）→ confirm payload，需用户二选一。"""
    items = []
    for tag, d in (("全局最低", track_a), ("常买Top3", track_b)):
        if not d:
            continue
        items.append({
            "name": f'{tag}｜{d.get("storeName") or d.get("storeCode")}',
            "price": d.get("price"),
            "note": d.get("savingDetail", ""),
        })
    return present.make_payload(
        "confirm", "双轨候选（请选一单）", "两条候选都算过价了，挑一条我备好下单卡。",
        metrics=[{"label": "候选", "value": len(items), "unit": "单"}],
        items=items,
        meta={"hint": "确认哪条后，我再生成该条的草稿并等你一键确认。"},
    )


# ---------------------------------------------------------------------------
# report：省钱账单（HTML 页面，屏内细看/存档）
# ---------------------------------------------------------------------------
_PERIOD_CN = {"today": "今天", "week": "这周", "month": "这月", "all": "历史"}


def report_from_ledger(agg: dict, nick: str = "老大") -> dict:
    period = agg.get("period", "month")
    return present.make_payload(
        "report", f'麦麦省钱账单（{_PERIOD_CN.get(period, period)}）',
        persona.on_savings_report(agg.get("saved", 0), agg.get("drops", 0),
                                  _PERIOD_CN.get(period, period)),
        metrics=[
            {"label": "累计省下", "value": agg.get("saved", 0), "unit": "元", "emphasis": True},
            {"label": "抓住降价", "value": agg.get("drops", 0), "unit": "次"},
            {"label": "确认订单", "value": agg.get("orders", 0), "unit": "单"},
            {"label": "涉及门店", "value": agg.get("stores", 0), "unit": "家"},
        ],
        items=agg.get("items") or [],
        meta={"hint": "账本只记本机（~/.mcd-skill/ledger.jsonl），drop 为盯价估算、order 为真实优惠。"},
    )


# ---------------------------------------------------------------------------
# share：省钱战报（图片，对外转发）
# ---------------------------------------------------------------------------
def share_from_draft(draft: dict, drops: int = 0, saved_total: float | None = None) -> dict:
    from .share_card import build_recipe
    saved = float(draft.get("discount") or 0)
    coupon = draft.get("coupon")
    metrics = []
    if saved > 0:
        metrics.append({"label": "本单省", "value": saved, "unit": "元", "emphasis": True})
        metrics.append({"label": "到手价", "value": draft.get("price"), "unit": "元"})
    else:
        metrics.append({"label": "到手价", "value": draft.get("price"), "unit": "元", "emphasis": True})
        metrics.append({"label": "原价", "value": draft.get("originalPrice"), "unit": "元"})
    if drops:
        metrics.append({"label": "本月抓住降价", "value": drops, "unit": "次"})
    coupon_txt = (f'用券「{coupon.get("name") or coupon.get("couponCode")}」'
                  if coupon else "本单未用券/积分")
    return present.make_payload(
        "share", "麦麦降价战报",
        f'{coupon_txt}，原价 {draft.get("originalPrice")} 到手 {draft.get("price")}。',
        metrics=metrics,
        items=[{"name": it.get("name") or it.get("productCode"),
                "quantity": it.get("quantity")} for it in draft.get("items", [])],
        recipe=build_recipe(draft),
        draft=draft,
        meta={"hint": "配方不含任何凭据；好友复现下单时同样需要他本人确认。"},
    )


# ---------------------------------------------------------------------------
# cancel：取消未支付订单（MONEY_WRITE，需人类确认闸）
# ---------------------------------------------------------------------------
def cancel_unpaid_preview(unpaid: list[dict], nick: str = "老大", scanned: int = 0) -> dict:
    """未支付订单预览 → confirm payload（确认后才会逐单取消）。"""
    items = []
    total = 0.0
    for o in unpaid:
        try:
            amt = float(o.get("realTotalAmount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        total += amt
        items.append({
            "name": o.get("storeName") or "未知门店",
            "price": amt,
            "note": f'订单 {o.get("orderId")} · {o.get("createTime") or ""} · {o.get("orderStatus") or ""}',
        })
    return present.make_payload(
        "confirm", "未支付订单 · 待确认取消",
        f'{nick}，扫到 {len(unpaid)} 单未支付订单（共 {scanned} 笔历史单），'
        f'合计 ¥{total:g}。取消后不可恢复，你确认我就逐单取消。',
        metrics=[
            {"label": "未支付订单", "value": len(unpaid), "unit": "单", "emphasis": True},
            {"label": "合计金额", "value": round(total, 2), "unit": "元"},
        ],
        items=items,
        actions=[
            {"id": "cancel_all", "label": "确认取消", "style": "primary",
             "command": 'python main.py cancel --yes --confirm-text "确认取消"'},
            {"id": "keep", "label": "先不动", "style": "ghost"},
        ],
        meta={"hint": "⛔ 取消不可逆。以上订单状态为「未支付」；若其中还有你要的单，先别确认。"},
    )


def cancel_unpaid_receipt(receipt: dict, nick: str = "老大") -> dict:
    """批量取消结果 → alert payload（逐单回执）。"""
    n_ok = len(receipt.get("cancelled") or [])
    n_fail = len(receipt.get("failed") or [])
    items = [{"name": f'订单 {c.get("orderId")}', "note": "已取消"}
             for c in (receipt.get("cancelled") or [])]
    items += [{"name": f'订单 {f.get("orderId")}', "note": f'取消失败：{f.get("error")}'}
              for f in (receipt.get("failed") or [])]
    summary = f'{nick}，已取消 {n_ok} 单' + (f'，{n_fail} 单失败' if n_fail else '。')
    return present.make_payload(
        "alert", "取消未支付订单 · 结果", summary,
        metrics=[
            {"label": "已取消", "value": n_ok, "unit": "单", "emphasis": True},
            {"label": "失败", "value": n_fail, "unit": "单"},
        ],
        items=items,
        meta={"hint": "取消结果已逐单列出；失败的单可稍后重试。"},
    )