# -*- coding: utf-8 -*-
"""业务对象 → 结构化 payload（14.3 原则①：核心逻辑只产出结构化数据）

渲染层（内联卡片 / HTML / 图片）一律不写在这里，由 present.render 分发。
"""
from __future__ import annotations

from . import present, persona, order_exec, coupons, price_memory


# ---------------------------------------------------------------------------
# alert：降价提醒 / 领券回执 / 到点提醒（内联卡片）
# ---------------------------------------------------------------------------
def alert_price_drop(events: list[dict], nick: str = "老大",
                     scanned: int = 0, scan_errors: int = 0) -> dict:
    """盯价巡检的降价/新低 → alert payload。"""
    events = price_memory.dedupe_price_events(events)
    new_lows = [e for e in events if e.get("type") in ("new_low", "dropped")]
    if not new_lows:
        if scan_errors and not scanned:
            return present.make_payload(
                "alert", "巡检失败",
                f"{nick}，尝试巡检的门店都没有成功读取菜单，不能判断当前是否有优惠。",
                metrics=[{"label": "巡检失败", "value": scan_errors, "unit": "家"}],
                meta={"hint": "检查网络、门店状态后重试。"},
            )
        if not scanned:
            return present.make_payload(
                "alert", "暂无可巡检门店",
                f"{nick}，还没有配置可巡检的门店。",
                meta={"hint": "先配置常去门店，或使用 --stores 指定门店。"},
            )
        hint = (f"部分门店巡检失败（{scan_errors} 家），本次结果不完整。"
                if scan_errors else "")
        return present.make_payload(
            "alert", "暂无新优惠", f"{nick}，这轮巡检没抓到降价，我继续盯着。",
            metrics=[{"label": "巡检门店", "value": scanned, "unit": "家"}],
            meta={"hint": hint} if hint else None,
        )
    items = []
    metrics = [{"label": "抓到降价", "value": len(new_lows), "unit": "个", "emphasis": True}]
    metrics.append({"label": "巡检门店", "value": scanned, "unit": "家"})
    saved = 0.0
    for e in new_lows[:8]:
        cur = float(e.get("currentPrice") or e.get("current") or e.get("price") or 0)
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
            float(new_lows[0].get("currentPrice") or new_lows[0].get("current") or
                  new_lows[0].get("price") or 0),
            new_lows[0].get("storeCode", "")),
        metrics=metrics, items=items,
        meta={"hint": "；".join(filter(None, [
            "要下单？跑 best 生成草稿，确认后才会真下单。",
            f"部分门店巡检失败（{scan_errors} 家），本次结果不完整。"
            if scan_errors else "",
        ]))},
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
    # claimedCount=None = 领前/领后卡包核验失败 → 数量未知，不能按 0 或候选数虚报
    n_raw = receipt.get("claimedCount")
    n_known = n_raw is not None
    n = int(n_raw or 0)
    wallet = receipt.get("walletCount")
    summary = (persona.on_coupons_claimed(n) if n_known and not receipt.get("errors")
               else f"{nick}，领券过程有报错，详见明细。")
    metrics = [{"label": "本次领到", "value": n if n_known else "未知（卡包核验失败）",
                "unit": "张", "emphasis": True}]
    if wallet is not None:
        metrics.append({"label": "卡包券数", "value": wallet, "unit": "张"})
    items = [{"name": c.get("name"), "note": c.get("label") or c.get("status") or ""}
             for c in claimable[:8]]

    # auto-bind 仍覆盖不到的券（多为活动营销券，官方未提供按券领取接口）→ 显式提示手动领
    manual = receipt.get("manualRequired")
    hint_bits = []
    images = []
    if isinstance(manual, list):
        if manual:
            metrics.append({"label": "待手动领取", "value": len(manual), "unit": "张",
                            "emphasis": False})
            names = "、".join(c.get("name") or "?" for c in manual[:6])
            items.append({"name": "需到麦当劳 App 手动领",
                          "note": f"需手动领取（auto-bind 未覆盖）：{names}"
                                  + (" 等" if len(manual) > 6 else "")})
            hint_bits.append(f"{len(manual)} 张活动营销券需手动领取（官方无按券领取接口，请点下方活动图片到 App 领）")
            # 把活动 banner 图一起带回，方便用户直接去领
            for c in manual:
                url = c.get("image") or ""
                if url:
                    images.append({"url": url, "name": c.get("name") or "活动券"})
        else:
            hint_bits.append("auto-bind 已覆盖全部可领券")
    elif manual is None:
        hint_bits.append("待手动领取清单未核验，请到 App 确认")

    base_hint = "；".join(receipt["errors"]) if receipt.get("errors") else "免费动作，已留回执。"
    extra = "；".join(hint_bits)
    meta_hint = "；".join([x for x in (base_hint, extra) if x])
    return present.make_payload(
        "alert", "领券回执", summary, metrics=metrics, items=items,
        images=images,
        meta={"hint": meta_hint or "免费动作，已留回执。"},
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
    if draft.get("addressText"):
        store_line += f'　配送至 {draft["addressText"]}'
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
        metrics.append({"label": "预计优惠", "value": saved, "unit": "元", "emphasis": True})
    # 外卖场景：展示配送费/打包费，让用户看清总价构成
    if draft.get("orderType") == 2 or draft.get("takeWay") == "delivery":
        if draft.get("deliveryFee"):
            metrics.append({"label": "配送费", "value": draft.get("deliveryFee"), "unit": "元"})
        if draft.get("packingFee"):
            metrics.append({"label": "打包费", "value": draft.get("packingFee"), "unit": "元"})

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
    hint_parts.append("这是下单前报价；优惠金额为预计值，未计入实际省下。")

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
            "savingsStatus": "quoted_not_purchased",
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


def confirm_candidates(track_a: dict, track_b: dict,
                       path_a: str = "", path_b: str = "") -> dict:
    """双轨候选（全局最低 vs 常买Top3）→ confirm payload，需用户二选一。

    path_a / path_b 提供时，为每条候选各生成一个「确认下单」动作，分别绑定
    对应草稿文件（确认哪条就执行哪条的 order 命令）；缺省则只出候选概要，
    由 Agent 追问用户选哪条后再出单卡确认。
    """
    items: list[dict] = []
    actions: list[dict] = []
    for tag, d, path, aid in (("全局最低", track_a, path_a, "confirm_a"),
                              ("常买Top3", track_b, path_b, "confirm_b")):
        if not d:
            continue
        items.append({
            "name": f'{tag}｜{d.get("storeName") or d.get("storeCode")}',
            "price": d.get("price"),
            "note": d.get("savingDetail", ""),
        })
        if path:
            actions.append({
                "id": aid,
                "label": f'确认{tag} ¥{d.get("price"):g}',
                "style": "primary" if aid == "confirm_a" else "ghost",
                "command": f'python main.py order --draft "{path}" '
                           f'--yes --confirm-text "确认下单"',
            })
    if not actions:
        actions.append({"id": "pick", "label": "选好后告诉我", "style": "primary"})
    return present.make_payload(
        "confirm", "双轨候选（请选一单）", "两条候选都算过价了，挑一条我备好下单卡。",
        metrics=[{"label": "候选", "value": len(items), "unit": "单"}],
        items=items,
        actions=actions,
        meta={"hint": "候选金额都是下单前预计报价，不计入实际省下；确认哪条就点哪条，未确认前我不会调用 create-order。",
              "savingsStatus": "quoted_not_purchased"},
    )


# ---------------------------------------------------------------------------
# report：省钱账单（HTML 页面，屏内细看/存档）
# ---------------------------------------------------------------------------
_PERIOD_CN = {"today": "今天", "week": "这周", "month": "这月", "all": "历史"}


def report_from_ledger(agg: dict, nick: str = "老大") -> dict:
    period = agg.get("period", "month")
    potential = float(agg.get("potential") or 0)
    metrics = [
        {"label": "实际省下", "value": agg.get("saved", 0), "unit": "元", "emphasis": True},
        {"label": "确认订单", "value": agg.get("orders", 0), "unit": "单"},
    ]
    unverified = int(agg.get("unverifiedOrders") or 0)
    if unverified:
        metrics.append({"label": "优惠待核实", "value": unverified, "unit": "单"})
    if potential > 0:
        metrics.append({"label": "潜在优惠(未购买)", "value": potential, "unit": "元"})
    metrics.append({"label": "抓住降价", "value": agg.get("drops", 0), "unit": "次"})
    metrics.append({"label": "涉及门店", "value": agg.get("stores", 0), "unit": "家"})
    return present.make_payload(
        "report", f'麦麦省钱账单（{_PERIOD_CN.get(period, period)}）',
        persona.on_savings_report(agg.get("saved", 0), agg.get("drops", 0),
                                  _PERIOD_CN.get(period, period), potential=potential,
                                  unverified=unverified),
        metrics=metrics,
        items=agg.get("items") or [],
        meta={"hint": "账本只保存在本机数据目录（默认 ~/.mcd-skill，可用 MCD_SKILL_DATA 修改）。"
                      "「实际省下」仅含 query-order.totalDiscountAmount 且订单已支付/已完成的优惠；"
                      "旧报价、待核实、取消和退款订单不计入。潜在优惠是未购买的盯价估算，二者不相加。"},
    )


# ---------------------------------------------------------------------------
# share：下单前优惠方案与复现配方（图片）
# ---------------------------------------------------------------------------
def share_from_draft(draft: dict, drops: int = 0) -> dict:
    from .share_card import build_recipe
    saved = float(draft.get("discount") or 0)
    coupon = draft.get("coupon")
    metrics = []
    if saved > 0:
        metrics.append({"label": "预计优惠（未下单）", "value": saved, "unit": "元", "emphasis": True})
        metrics.append({"label": "到手价", "value": draft.get("price"), "unit": "元"})
    else:
        metrics.append({"label": "到手价", "value": draft.get("price"), "unit": "元", "emphasis": True})
        metrics.append({"label": "原价", "value": draft.get("originalPrice"), "unit": "元"})
    if drops:
        metrics.append({"label": "本月抓住降价", "value": drops, "unit": "次"})
    coupon_txt = (f'用券「{coupon.get("name") or coupon.get("couponCode")}」'
                  if coupon else "本单未用券/积分")
    return present.make_payload(
        "share", "麦麦优惠方案（待下单）",
        f'{coupon_txt}，原价 {draft.get("originalPrice")}，报价到手 {draft.get("price")}。'
        "当前仍是待下单报价，未计入「实际省下」。",
        metrics=metrics,
        items=[{"name": it.get("name") or it.get("productCode"),
                "quantity": it.get("quantity")} for it in draft.get("items", [])],
        recipe=build_recipe(draft),
        draft=draft,
        meta={"hint": "这是未下单报价；配方不含凭据，好友复现下单时同样需要他本人确认。",
              "savingsStatus": "quoted_not_purchased"},
    )


# ---------------------------------------------------------------------------
# cancel：取消未支付订单（MONEY_WRITE，需人类确认闸）
# ---------------------------------------------------------------------------
def cancel_unpaid_preview(unpaid: list[dict], nick: str = "老大", scanned: int = 0) -> dict:
    """未支付订单预览 → confirm payload（确认后才会逐单取消）。

    说明：order-list 无分页、只返回近期订单，故本预览只能覆盖「近期可见」范围，
    更早的未支付单可能未被扫描到，需明确提示。
    """
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
        f'{nick}，近期可见的 {scanned} 笔订单中扫到 {len(unpaid)} 单未支付，'
        f'合计 ¥{total:g}。取消后不可恢复，你确认我就逐单取消。',
        metrics=[
            {"label": "未支付订单", "value": len(unpaid), "unit": "单", "emphasis": True},
            {"label": "合计金额", "value": round(total, 2), "unit": "元"},
            {"label": "扫描范围", "value": scanned, "unit": "笔近期单"},
        ],
        items=items,
        actions=[
            {"id": "cancel_all", "label": "确认取消", "style": "primary",
             "command": 'python main.py cancel --yes --confirm-text "确认取消"'},
            {"id": "keep", "label": "先不动", "style": "ghost"},
        ],
        meta={"hint": "⛔ 取消不可逆。接口仅返回近期订单，更早的未支付单可能未被覆盖，请以 App 为准；"
                      "若其中还有你要的单，先别确认。"},
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
