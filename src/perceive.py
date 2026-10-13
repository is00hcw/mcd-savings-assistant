# -*- coding: utf-8 -*-
"""B · 主动感知巡检

常去门店 query-meals → 按归一化 name 跨店比价 → 检出新低/降价/新特价；
顺带检测：可领券、券生效、临期积分。
"""
from .config import get_config
from .normalize import meal_index_from_query_meals
from . import price_memory, ledger


def scan_stores(client, store_codes: list[str] | None = None,
                record_ledger: bool = True) -> dict:
    """巡检若干门店，返回巡检报告（不触发任何写操作）。"""
    cfg = get_config()
    codes = list(store_codes or cfg.favorite_stores)[: cfg.scan_store_limit]
    report = {
        "scannedStores": [],
        "priceEvents": [],        # 新低/处于历史低位
        "crossStoreCheapest": {}, # name -> {storeCode, price}
        "newCouponsAvailable": [],
        "expirePoints": None,
        "campaigns": [],
    }

    # 临期积分
    try:
        acct = client.my_account()
        a = acct.get("data") if isinstance(acct, dict) else acct
        if isinstance(a, dict):
            cur = a.get("currentMouthExpirePoint") or 0
            nxt = a.get("nextMouthExpirePoint") or 0
            report["expirePoints"] = {
                "currentMonth": cur, "nextMonth": nxt,
                "available": a.get("availablePoint"),
                "alert": float(cur or 0) >= cfg.expire_points_threshold,
            }
    except Exception as e:
        report["expirePointsError"] = str(e)

    # 可领券（只看不领，领券走 coupons 命令）
    # 实测结构：data[] = {couponName, couponStatus, label}
    try:
        avail = client.available_coupons()
        al = avail.get("data") if isinstance(avail, dict) else avail
        items = al if isinstance(al, list) else (al or {}).get("list", []) if isinstance(al, dict) else []
        report["newCouponsAvailable"] = [
            {"name": c.get("couponName") or c.get("name") or c.get("title"),
             "status": c.get("couponStatus") or "", "label": c.get("label") or ""}
            for c in items
        ]
    except Exception as e:
        report["couponsAvailableError"] = str(e)

    # 本月活动（品牌级）
    try:
        cal = client.call("campaign-calendar")
        cd = cal.get("data") if isinstance(cal, dict) else cal
        if isinstance(cd, dict):
            report["campaigns"] = cd.get("list") or cd.get("campaigns") or []
    except Exception as e:
        report["campaignError"] = str(e)

    # 逐店菜单比价
    for code in codes:
        try:
            meals_raw = client.meals(code)
            md = meals_raw.get("data") if isinstance(meals_raw, dict) else meals_raw
            idx = meal_index_from_query_meals(md if isinstance(md, dict) else {})
        except Exception as e:
            report["scannedStores"].append({"storeCode": code, "error": str(e)})
            continue

        # 快照对比（跨期降价检测）
        prev = price_memory.load_snapshot(code)
        if prev:
            for k, m in idx.items():
                pm = (prev.get("meals") or {}).get(k)
                if not pm:
                    continue
                try:
                    prev_p = float(pm.get("currentPrice") or 0)
                    cur_p = float(m.get("currentPrice") or 0)
                except (TypeError, ValueError):
                    continue
                if prev_p and cur_p and cur_p < prev_p:
                    report["priceEvents"].append({
                        "type": "dropped", "name": m.get("name") or k,
                        "prev": prev_p, "current": cur_p, "storeCode": code,
                    })

        # 刷新基线 + 低价事件
        events = price_memory.update_from_meals(idx, code)
        report["priceEvents"].extend(events)
        price_memory.save_snapshot(code, idx)
        price_memory.merge_profile_from_meals(idx, code)

        # 降价事件入账本（H 省钱账单数据源，估算值）
        if record_ledger:
            for ev in events:
                if ev.get("type") not in ("new_low", "dropped"):
                    continue
                prev = float(ev.get("prevMin") or ev.get("prev") or 0)
                cur = float(ev.get("currentPrice") or 0)
                if prev and cur and prev > cur:
                    try:
                        ledger.append_event(
                            "drop", name=ev.get("name"), storeCode=code,
                            prevPrice=prev, price=cur, saved=round(prev - cur, 2),
                            source=ev.get("type"),
                        )
                    except Exception:
                        pass  # 账本失败不影响巡检主流程

        # 跨店最便宜登记
        for k, m in idx.items():
            try:
                p = float(m.get("currentPrice") or 0)
            except (TypeError, ValueError):
                continue
            if p <= 0:
                continue
            best = report["crossStoreCheapest"].get(k)
            if best is None or p < best["price"]:
                report["crossStoreCheapest"][k] = {
                    "storeCode": code, "price": p,
                    "name": m.get("name") or k,
                    "originalPrice": m.get("originalPrice"),
                }
        report["scannedStores"].append({"storeCode": code, "meals": len(idx)})

    # 汇总：处于历史低位的商品（across scanned stores）
    baseline = price_memory.load_baseline()
    lows = []
    for k, best in report["crossStoreCheapest"].items():
        # 只推真优惠价（现价<原价），无折扣常价不上榜
        try:
            if not (best.get("originalPrice") and
                    float(best["price"]) < float(best["originalPrice"])):
                continue
        except (TypeError, ValueError):
            continue
        rec = baseline.get(k)
        if rec and float(rec.get("minCurrentPrice") or 0) > 0:
            if best["price"] <= float(rec["minCurrentPrice"]) * cfg.new_low_ratio:
                lows.append(best)
    report["atHistoricalLow"] = sorted(lows, key=lambda x: x["price"])[:10]
    return report
