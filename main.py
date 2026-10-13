#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦省钱助理 · CLI 入口

零第三方依赖，仅需 Python 3.9+ 标准库。
Token 读取顺序：环境变量 MCD_MCP_TOKEN > config.local.json（不进 git）。

红线：create-order 必须先 `best` 生成草稿 → 用户明确确认 →
`order --draft <file> --yes --confirm-text "..."` 执行。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.version_info < (3, 9):
    print("本工具仅需 Python 3.9+ 标准库、无第三方依赖，请升级 Python。")
    sys.exit(3)

from src.config import get_config                      # noqa: E402
from src.mcd_client import McdClient, McpError          # noqa: E402
from src import (price_memory, perceive, optimize,   # noqa: E402
                 coupons as coupons_mod, order_exec, persona, share_card)


def _client() -> McdClient:
    cfg = get_config()
    return McdClient(cfg.token)


def _print(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
def cmd_warmup(args):
    print(persona.greeting())
    client = _client()
    result = price_memory.cold_start_from_orders(client)
    if result.get("error"):
        print(f"[warmup] 拉取订单失败: {result['error']}")
        return
    profile = result["profile"]
    top = price_memory.top_products(3)
    print(f"[warmup] 已导入 {sum(int(v.get('buyCount') or 0) for v in profile.values())} 件商品购买记录")
    print(f"[warmup] 买过优惠的商品（订单级佐证）: {len(result['discounted_names'])} 个")
    print(f"[warmup] 购买量 Top3: {top}")
    cfg = get_config()
    gaps = cfg.personal_gaps()
    if gaps:
        print("\n[提醒] 以下个人信息待补充到 config.local.json（不会提交 git）:")
        for g in gaps:
            print(f"  - {g}")


def cmd_patrol(args):
    client = _client()
    report = perceive.scan_stores(client, args.stores or None)
    for ev in report["priceEvents"]:
        if ev["type"] == "new_low":
            print(persona.on_price_drop(ev["name"], ev["prevMin"], ev["currentPrice"], ev.get("storeCode", "")))
        elif ev["type"] == "dropped":
            print(persona.on_price_drop(ev["name"], ev["prev"], ev["current"], ev.get("storeCode", "")))
    lows = report.get("atHistoricalLow") or []
    if lows:
        print("\n处于历史低价的商品:")
        for x in lows:
            print(f"  {x['name']}: ¥{x['price']:g}（原价 ¥{x.get('originalPrice') or '?'}）@ {x['storeCode']}")
    ep = report.get("expirePoints")
    if ep and ep.get("alert"):
        print(f"\n[积分] 本月将有 {ep['currentMonth']} 分过期，可用余额 {ep['available']}，建议去积分商城兑换。")
    avail = report.get("newCouponsAvailable") or []
    if avail:
        print(f"\n[领券] 有 {len(avail)} 张券可领，运行 `python main.py coupons` 一键领取。")
    if not (report["priceEvents"] or lows or avail):
        print(f"{persona.nick()}，暂无新优惠，我继续盯着。")
    if args.json:
        _print(report)


def cmd_stores(args):
    client = _client()
    address = args.address or get_config().default_address
    if not address:
        print("请提供地址：--address \"...\" 或在 config.local.json 填 user.default_address")
        sys.exit(1)
    _print(client.nearby_stores(address))


def cmd_meals(args):
    client = _client()
    raw = client.meals(args.store)
    d = raw.get("data") if isinstance(raw, dict) else raw
    from src.normalize import meal_index_from_query_meals
    idx = meal_index_from_query_meals(d if isinstance(d, dict) else {})
    rows = []
    for m in idx.values():
        try:
            cur = float(m.get("currentPrice") or 0)
            orig = float(m.get("originalPrice") or 0)
        except (TypeError, ValueError):
            continue
        rows.append((cur, orig, m))
    rows.sort(key=lambda r: r[0])
    print(f"{'现价':>8} {'原价':>8}  商品（门店 {args.store}，共 {len(rows)} 个有价商品）")
    for cur, orig, m in rows:
        tag = " ←" if orig and cur < orig * 0.8 else ""
        print(f"{cur:>8.2f} {orig:>8.2f}  {m.get('name')}  [{m.get('code')}]{tag}")
    if args.save:
        price_memory.update_from_meals(idx, args.store)
        price_memory.save_snapshot(args.store, idx)
        print("(已刷新低价基线与快照)")


def cmd_coupons(args):
    client = _client()
    receipt = coupons_mod.auto_claim(client)
    n = len(receipt.get("available") or [])
    if receipt["errors"]:
        print("[领券] 出现错误:", "; ".join(receipt["errors"]))
    else:
        print(persona.on_coupons_claimed(n))
    _print(receipt)


def cmd_best(args):
    client = _client()
    wanted = []
    for part in args.items.split(","):
        p = part.strip()
        if not p:
            continue
        if "x" in p.lower():
            name, qty = p.lower().split("x", 1)
            wanted.append((name.strip(), int(qty or 1)))
        else:
            wanted.append((p, 1))
    try:
        draft = optimize.optimize(client, args.store, wanted, track=args.track)
    except (ValueError, McpError) as e:
        print(f"[best] 失败: {e}")
        sys.exit(1)
    path = order_exec.save_draft(draft)
    print(persona.confirm_gate(draft))
    print(f"\n订单草稿已保存: {path}")
    print(optimize.draft_to_json(draft))
    print("\n确认后执行: python main.py order --draft "
          f"\"{path.name}\" --yes --confirm-text \"确认下单\"")


def cmd_order(args):
    """下单（红线闸）。"""
    draft_path = Path(order_exec.DRAFT_DIR) / args.draft
    if not draft_path.exists():
        draft_path = Path(args.draft)
    draft = order_exec.load_draft(draft_path)
    if not args.yes:
        print(persona.blocked_by_gate())
        print(optimize.draft_to_json(draft))
        sys.exit(4)
    if not args.confirm_text:
        print("需要 --confirm-text \"确认下单\" 作为显式确认凭据。")
        sys.exit(4)
    draft["confirmedByHuman"] = True
    draft["confirmedAt"] = sys_date()
    draft["confirmedText"] = args.confirm_text
    if not order_exec.human_gate(draft):
        print(persona.blocked_by_gate())
        sys.exit(4)
    receipt = order_exec.execute_draft(_client(), draft)
    print("下单结果:")
    _print(receipt)


def sys_date():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def cmd_share(args):
    draft = order_exec.load_draft(Path(order_exec.DRAFT_DIR) / args.draft
                                  if not Path(args.draft).exists() else Path(args.draft))
    html = share_card.render_card(draft, drops=args.drops)
    out = Path(args.out or f"share-{args.draft.replace('.json', '')}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"战报卡已生成: {out.resolve()}")
    print(f"\n好友复现提示词:\n{share_card.recipe_prompt(draft)}")


def cmd_raw(args):
    """只读调试：直接调用任意只读工具。"""
    client = _client()
    kwargs = json.loads(args.args) if args.args else {}
    _print(client.call(args.tool, kwargs))


def main():
    ap = argparse.ArgumentParser(prog="mcd-skill", description="麦麦省钱助理 · 你的省钱盯价闹钟")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("warmup", help="首启热身：导入历史订单建低价档案（只读）")
    p.set_defaults(fn=cmd_warmup)

    p = sub.add_parser("patrol", help="盯价巡检（只读）")
    p.add_argument("--stores", nargs="*", help="门店 code 列表，默认用配置")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_patrol)

    p = sub.add_parser("stores", help="查附近门店（只读）")
    p.add_argument("--address", default="")
    p.set_defaults(fn=cmd_stores)

    p = sub.add_parser("meals", help="看某门店菜单优惠价（只读）")
    p.add_argument("--store", required=True)
    p.add_argument("--save", action="store_true", help="同时刷新低价基线")
    p.set_defaults(fn=cmd_meals)

    p = sub.add_parser("coupons", help="自动领券（免费写，留回执）")
    p.set_defaults(fn=cmd_coupons)

    p = sub.add_parser("best", help="算最优价并生成订单草稿（只读）")
    p.add_argument("--store", required=True)
    p.add_argument("--items", required=True, help='"商品名或code x数量,..."')
    p.add_argument("--track", choices=["global", "top3"], default="global")
    p.set_defaults(fn=cmd_best)

    p = sub.add_parser("order", help="下单（红线：需 --yes --confirm-text）")
    p.add_argument("--draft", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--confirm-text", dest="confirm_text", default="")
    p.set_defaults(fn=cmd_order)

    p = sub.add_parser("share", help="生成省钱战报分享卡")
    p.add_argument("--draft", required=True)
    p.add_argument("--drops", type=int, default=0)
    p.add_argument("--out", default="")
    p.set_defaults(fn=cmd_share)

    p = sub.add_parser("raw", help="调用任意只读工具（调试）")
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="")
    p.set_defaults(fn=cmd_raw)

    args = ap.parse_args()
    try:
        args.fn(args)
    except McpError as e:
        print(f"[MCP 错误] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
