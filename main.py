#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦省钱助理 · CLI 入口

零第三方依赖，仅需 Python 3.9+ 标准库。
Token 读取顺序：环境变量 MCD_MCP_TOKEN > config.local.json（已 gitignore，不进仓库）。

输出形态（14.3）：所有命令先产出**结构化 payload**（type: alert|confirm|report|share），
再按 --format 渲染：inline（内联卡片/Markdown 兜底）| json | html | svg（图片）。
同一数据源多渲染，渲染层可插拔。

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
from src import (price_memory, perceive, optimize,     # noqa: E402
                 coupons as coupons_mod, order_exec, persona, payloads,
                 present, ledger, share_card, address, onboarding)

FORMATS = ("inline", "json", "html", "svg")


def _client() -> McdClient:
    return McdClient(get_config().token)


def _print(data):
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _show(payload: dict, args, out: str = "") -> None:
    """统一出口：payload → 渲染 → 控制台/文件。"""
    r = present.emit(payload, getattr(args, "format", "inline"), out or None)
    if r["format"] in ("html", "svg"):
        target = out or f"data/{payload['type']}-{r['format']}.{r['format']}"
        if not out:
            present.emit(payload, r["format"], target)
            r["path"] = target
        print(f"[{payload['type']}] 已生成 {r['format']} 产物: {r.get('path')}")
        print("（用浏览器/图片查看器打开即可；内容与内联卡片同源）")
    else:
        print(r["content"])


# ---------------------------------------------------------------------------
def cmd_phrase(args):
    """话术 → 命令映射：给新用户「照着说」，也给 Agent 查映射表。

    花钱类意图永远只回显命令，不自动执行（红线）。
    """
    from src import phrases as ph
    if not args.text:
        print(ph.table_markdown())
        return
    hit = ph.match(args.text)
    if not hit:
        print(f"没匹配到已知意图。可以看看完整话术表：python main.py phrase")
        print("或直接问：python main.py capabilities")
        return
    print(f"你说的是：「{args.text}」")
    print(f"→ 意图：{hit['intent']}　渲染：{hit['type'] or '纯文本'}")
    print(f"→ 命令：{hit['cmd']}")
    print(f"→ 说明：{hit['note']}")
    if not ph.runnable(hit["intent"]):
        print("⛔ 该意图涉及写入/花钱动作，需用户本人明确确认后才执行，这里只回显命令。")
        return
    if args.run:
        print("→ 立即执行：")
        import subprocess
        cmd = hit["cmd"].replace("<门店code>", args.store or "1421252")
        cmd = cmd.replace("<商品>", args.item or "麦旋风").replace("<> x1", " x1")
        cmd = cmd.replace("<草稿.json>", args.draft or "")
        subprocess.run(cmd, shell=True)


def cmd_init(args):
    """一键生成 config.local.json 模板，并打印领 Token 指引。"""
    from src import config as cfgmod
    if cfgmod.LOCAL_CONFIG.exists() and not args.force:
        print(f"已存在 {cfgmod.LOCAL_CONFIG}，不会覆盖（如需重建加 --force）")
    else:
        cfgmod.LOCAL_CONFIG.write_text(
            cfgmod.EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"✅ 已生成配置模板：{cfgmod.LOCAL_CONFIG}")
    print()
    print(f"① 领 Token：手机号登录 https://open.mcd.cn/mcp → 右上角【控制台】→ 点【激活】→ 复制 Token")
    print("② 把 Token 填进上面文件的 mcp_token 字段")
    print("③ 自检：python main.py doctor   →   热身：python main.py warmup")
    print("（config.local.json 已被 .gitignore 排除，Token 不会提交到仓库）")


def cmd_doctor(args):
    """首次使用自检（只读）：环境 → 配置 → Token → 地址/门店就绪 → 下一步。"""
    res = onboarding.doctor(check_token=not args.offline)
    print(res["text"])
    if getattr(args, "format", "inline") == "json":
        _print(res)
    sys.exit(0 if res["ready"] else 1)


def cmd_capabilities(args):
    """Agent 自述元信息：我是谁 / 我会什么 / 我绝不做什么。"""
    if getattr(args, "format", "inline") == "json":
        _print(onboarding.capabilities())
    else:
        print(onboarding.capabilities_text())


def cmd_warmup(args):
    print(persona.greeting())
    client = _client()
    cfg = get_config()

    # 常用地址自动推导（外卖地址为权威来源，订单门店地址兜底）
    if not cfg.default_address or args.derive_address:
        try:
            derived = address.derive_addresses(client)
            print("\n" + address.describe(derived))
            if derived.get("primary"):
                patch = address.to_config_patch(derived)
                if args.apply_address:
                    written = cfg.save_user_fields(**patch)
                    print(f"\n[已写入 config.local.json（不进 git）] {written}")
                else:
                    print(f"\n[未写入] 如认可上面推导的地址，加 --apply-address 自动回填："
                          f"python main.py warmup --apply-address")
        except Exception as e:
            print(f"[地址推导] 跳过: {e}")

    result = price_memory.cold_start_from_orders(client)
    if result.get("error"):
        print(f"[warmup] 拉取订单失败: {result['error']}")
        return
    profile = result["profile"]
    top = price_memory.top_products(3)
    print(f"[warmup] 已导入 {sum(int(v.get('buyCount') or 0) for v in profile.values())} 件商品购买记录")
    print(f"[warmup] 买过优惠的商品（订单级佐证）: {len(result['discounted_names'])} 个")
    print(f"[warmup] 购买量 Top3: {top}")

    # 常去门店也可从订单推导（若配置里没有）
    if not cfg.favorite_stores:
        scs = list(result.get("orderStores") or [])[: cfg.scan_store_limit]
        if scs:
            print(f"\n[建议] 从订单推导出你的常去门店: {scs}")
            if args.apply_stores:
                written = cfg.save_user_fields(favorite_stores=scs)
                print(f"[已写入 config.local.json（不进 git）] {written}")
            else:
                print("  认可就加 --apply-stores 自动回填：python main.py warmup --apply-stores")

    gaps = get_config().personal_gaps()
    if gaps:
        print("\n[提醒] 以下个人信息待补充到 config.local.json（不会提交 git）:")
        for g in gaps:
            print(f"  - {g}")


def cmd_address(args):
    """常用地址推导（默认只读预览，不写盘）。"""
    derived = address.derive_addresses(_client())
    print(address.describe(derived))
    if args.apply and derived.get("primary"):
        written = get_config().save_user_fields(**address.to_config_patch(derived))
        print(f"\n[已写入 config.local.json（已被 gitignore）] {written}")
    elif derived.get("primary"):
        print(f"\n[未写入] 认可就加 --apply：python main.py address --apply")


def cmd_patrol(args):
    """B 主动感知：巡检 → alert payload（内联卡片）。"""
    report = perceive.scan_stores(_client(), args.stores or None)
    nick = get_config().nickname
    scanned = len(report.get("scannedStores") or [])
    payloads_out = [payloads.alert_price_drop(report["priceEvents"], nick, scanned)]
    ep = payloads.alert_expiring_points(report.get("expirePoints"), nick)
    if ep:
        payloads_out.append(ep)
    avail = [c for c in (report.get("newCouponsAvailable") or [])
             if "已领取" not in str(c.get("label") or "")]
    if avail and not getattr(args, "no_coupon_hint", False):
        payloads_out.append(present.make_payload(
            "alert", "有券可领",
            f'{nick}，{len(avail)} 张券在等你，一键领进卡包不领白不领。',
            metrics=[{"label": "可领券", "value": len(avail), "unit": "张", "emphasis": True}],
            items=[{"name": c.get("name"), "note": c.get("label") or ""} for c in avail[:6]],
            meta={"hint": "领券是免费动作：python main.py coupons --claim --yes"},
        ))

    if args.format == "json":
        _print({"payloads": payloads_out, "report": report})
        return
    for p in payloads_out:
        _show(p, args)
        print()


def cmd_stores(args):
    """查附近门店：地址优先级 = 命令行 > 配置 > 自动推导（外卖地址/门店锚点）。"""
    cfg = get_config()
    address_text = args.address or cfg.default_address
    origin = "--address" if args.address else ("config" if cfg.default_address else "")
    if not address_text:
        try:
            derived = address.derive_addresses(_client())
            address_text = derived.get("primary") or (
                (derived.get("storeAnchor") or {}).get("storeAddress") or "")
            origin = "自动推导（外卖地址）" if derived.get("primary") else (
                "自动推导（门店地址兜底）" if address_text else "")
            if address_text:
                print(f"[地址来源] {origin}：{address_text}")
        except Exception as e:
            print(f"[地址推导失败] {e}")
    if not address_text:
        print('请提供地址：--address "..."、在 config.local.json 填 user.default_address，'
              '或先跑 python main.py address --apply')
        sys.exit(1)
    if not args.address and origin == "自动推导（外卖地址）" and args.save_derived:
        print(get_config().save_user_fields(default_address=address_text))
    _print(_client().nearby_stores(address_text))


def cmd_meals(args):
    client = _client()
    raw = client.meals(args.store)
    d = raw.get("data") if isinstance(raw, dict) else raw
    from src.normalize import meal_index_from_query_meals
    idx = meal_index_from_query_meals(d if isinstance(d, dict) else {})
    rows = []
    for m in idx.values():
        try:
            cur, orig = float(m.get("currentPrice") or 0), float(m.get("originalPrice") or 0)
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
    """C 自动领券：默认**只读预览**，--claim --yes 才真正领取（免费写操作）。"""
    client = _client()
    if not args.claim:
        try:
            avail = client.available_coupons()
            d = avail.get("data") if isinstance(avail, dict) else avail
            items = d if isinstance(d, list) else (d or {}).get("list", [])
        except Exception as e:
            print(f"[领券预览] 失败: {e}")
            sys.exit(1)
        claimable = [c for c in items if "已领取" not in str(c.get("label") or "")]
        p = present.make_payload(
            "alert", "可领券预览（未领取）",
            f'{get_config().nickname}，这些券现在能领，我没动你的账户。',
            metrics=[{"label": "可领取", "value": len(claimable), "unit": "张", "emphasis": True},
                     {"label": "活动券总数", "value": len(items), "unit": "张"}],
            items=[{"name": c.get("couponName") or c.get("name"),
                    "note": c.get("label") or c.get("couponStatus") or ""} for c in claimable[:8]],
            meta={"hint": "真要领：python main.py coupons --claim --yes（免费动作，仍留回执）"},
        )
        _show(p, args)
        return
    if not args.yes:
        print("领券会写入你的账户，需显式确认：加 --claim --yes 再执行。")
        sys.exit(4)
    receipt = coupons_mod.auto_claim(client)
    _show(payloads.alert_coupon_receipt(receipt, get_config().nickname), args)


def cmd_best(args):
    """D 最优下单 → confirm payload（确认闸交互卡片）。"""
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
        draft = optimize.optimize(_client(), args.store, wanted, track=args.track)
    except (ValueError, McpError) as e:
        print(f"[best] 失败: {e}")
        sys.exit(1)
    path = order_exec.save_draft(draft)
    confirm_payload = payloads.confirm_from_draft(draft, str(path))
    if args.format == "json":
        text = json.dumps(confirm_payload, ensure_ascii=False, indent=2)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"[confirm] payload 已保存: {out.resolve()}")
            print(f"草稿: {path}")
        else:
            print(text)
            print(f"\n草稿: {path}")
        return
    _show(confirm_payload, args, out=args.out)
    if not (args.out and args.format in ("html", "svg")):
        print(f"\n订单草稿已保存: {path}")
        print(optimize.draft_to_json(draft))


def cmd_order(args):
    """下单（红线闸）：无 --yes 一律拒绝。"""
    draft_path = Path(order_exec.DRAFT_DIR) / args.draft
    if not draft_path.exists():
        draft_path = Path(args.draft)
    draft = order_exec.load_draft(draft_path)
    if not args.yes:
        print(persona.blocked_by_gate())
        print(optimize.draft_to_json(draft))
        sys.exit(4)
    if not args.confirm_text:
        print('需要 --confirm-text "确认下单" 作为显式确认凭据。')
        sys.exit(4)
    draft["confirmedByHuman"] = True
    draft["confirmedAt"] = sys_date()
    draft["confirmedText"] = args.confirm_text
    if not order_exec.human_gate(draft):
        print(persona.blocked_by_gate())
        sys.exit(4)
    receipt = order_exec.execute_draft(_client(), draft)
    print("下单结果（真实优惠已入账本）:")
    _print(receipt)


def cmd_report(args):
    """H 省钱账单 → report payload（HTML 页面，细看/存档）。"""
    agg = ledger.aggregate(args.period)
    p = payloads.report_from_ledger(agg, get_config().nickname)
    if args.format == "json":
        _print({"payload": p, "ledger": agg})
        return
    _show(p, args, out=args.out)
    if agg.get("events"):
        print(f"\n账本事件 {len(agg['events'])} 条（{ledger.LEDGER_FILE}）")


def cmd_share(args):
    """I 省钱战报 → share payload（图片，对外转发）。"""
    draft_path = Path(order_exec.DRAFT_DIR) / args.draft
    if not draft_path.exists():
        draft_path = Path(args.draft)
    draft = order_exec.load_draft(draft_path)
    agg = ledger.aggregate("month")
    p = payloads.share_from_draft(draft, drops=args.drops or agg.get("drops", 0))
    if args.format == "json":
        _print(p)
        return
    _show(p, args, out=args.out)
    print(f"\n好友复现提示词:\n{share_card.recipe_prompt(draft)}")


def cmd_render(args):
    """渲染层可插拔验证：任意 payload → 任意形态。"""
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    if "type" not in payload:
        print("payload 缺少 type（alert|confirm|report|share）")
        sys.exit(1)
    _show(payload, args, out=args.out)


def cmd_raw(args):
    """只读调试：直接调任意只读工具。"""
    kwargs = json.loads(args.args) if args.args else {}
    _print(_client().call(args.tool, kwargs))


def sys_date():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(
        prog="mcd-skill",
        description="麦麦省钱助理 · 你的省钱盯价闹钟（输出 alert|confirm|report|share 四型）",
        epilog="首次使用：python main.py doctor 自检；不确定能干什么看 python main.py capabilities")
    sub = ap.add_subparsers(dest="cmd", required=False)

    def add_fmt(p, default="inline"):
        p.add_argument("--format", choices=FORMATS, default=default,
                       help="渲染形态：inline 内联卡片(默认) / json 结构化 / html 页面 / svg 图片")
        p.add_argument("--out", default="", help="输出文件路径（html/svg 时生效）")

    p = sub.add_parser("init", help="生成 config.local.json 模板 + 领 Token 指引")
    p.add_argument("--force", action="store_true", help="覆盖已存在的配置")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("phrase", help="常用话术 → 命令映射（不传参数则打印话术表）")
    p.add_argument("text", nargs="?", default="", help="用户说的一句话，如「麦当劳有什么优惠」")
    p.add_argument("--run", action="store_true", help="只读意图可直接执行；花钱意图一律拒绝")
    p.add_argument("--store", default="", help="配合 --run 补门店 code")
    p.add_argument("--item", default="", help="配合 --run 补商品名")
    p.add_argument("--draft", default="", help="配合 --run 补草稿文件名")
    p.set_defaults(fn=cmd_phrase)

    p = sub.add_parser("doctor", help="首次使用自检（只读，不下单不领券）")
    p.add_argument("--offline", action="store_true", help="跳过 Token 连通性探测（离线可用）")
    add_fmt(p)
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("capabilities", help="能力清单与红线（给 Agent 读的自述元信息）")
    add_fmt(p)
    p.set_defaults(fn=cmd_capabilities)

    p = sub.add_parser("warmup", help="首启热身：导入历史订单 + 推导地址（只读，--apply 才写盘）")
    p.add_argument("--derive-address", action="store_true", help="即使已有地址也重新推导")
    p.add_argument("--apply-address", action="store_true", help="把推导出的地址写入 config.local.json")
    p.add_argument("--apply-stores", action="store_true", help="把订单里常去门店写入 config.local.json")
    p.set_defaults(fn=cmd_warmup)

    p = sub.add_parser("address", help="常用地址推导（默认只读预览，--apply 回填）")
    p.add_argument("--apply", action="store_true", help="写入 config.local.json（本地文件，不进 git）")
    p.set_defaults(fn=cmd_address)

    p = sub.add_parser("patrol", help="盯价巡检 → alert 卡片（只读）")
    p.add_argument("--stores", nargs="*", help="门店 code 列表，默认用配置")
    add_fmt(p)
    p.set_defaults(fn=cmd_patrol)

    p = sub.add_parser("stores", help="查附近门店（地址缺失时自动推导，只读）")
    p.add_argument("--address", default="")
    p.add_argument("--save-derived", action="store_true",
                   help="把自动推导的地址写入 config.local.json")
    p.set_defaults(fn=cmd_stores)

    p = sub.add_parser("meals", help="看某门店菜单优惠价（只读）")
    p.add_argument("--store", required=True)
    p.add_argument("--save", action="store_true", help="同时刷新低价基线")
    p.set_defaults(fn=cmd_meals)

    p = sub.add_parser("coupons", help="可领券预览（默认只读）/ --claim 才领")
    p.add_argument("--claim", action="store_true", help="真正领取（免费写操作）")
    p.add_argument("--yes", action="store_true", help="确认领取")
    add_fmt(p)
    p.set_defaults(fn=cmd_coupons)

    p = sub.add_parser("best", help="算最优价 → confirm 确认卡（只读）")
    p.add_argument("--store", required=True)
    p.add_argument("--items", required=True, help='"商品名或code x数量,..."')
    p.add_argument("--track", choices=["global", "top3"], default="global")
    add_fmt(p)
    p.set_defaults(fn=cmd_best)

    p = sub.add_parser("order", help="下单（红线：需 --yes --confirm-text）")
    p.add_argument("--draft", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--confirm-text", dest="confirm_text", default="")
    p.set_defaults(fn=cmd_order)

    p = sub.add_parser("report", help="省钱账单 → report HTML 页面")
    p.add_argument("--period", choices=["today", "week", "month", "all"], default="month")
    add_fmt(p, default="html")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("share", help="省钱战报 → share 图片")
    p.add_argument("--draft", required=True)
    p.add_argument("--drops", type=int, default=0)
    add_fmt(p, default="svg")
    p.set_defaults(fn=cmd_share)

    p = sub.add_parser("render", help="任意 payload → 任意形态（渲染层可插拔验证）")
    p.add_argument("--payload", required=True)
    add_fmt(p)
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("raw", help="调用任意只读工具（调试）")
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="")
    p.set_defaults(fn=cmd_raw)

    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        # 新用户/新会话的第一屏：给中文引导，不要 argparse usage 报错糊脸
        print(onboarding.welcome())
        return
    try:
        args.fn(args)
    except McpError as e:
        print(f"[MCP 错误] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()