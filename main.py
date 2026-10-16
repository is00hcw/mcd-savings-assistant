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
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.version_info < (3, 9):
    print("本工具仅需 Python 3.9+ 标准库、无第三方依赖，请升级 Python。")
    sys.exit(3)

from src.config import get_config                      # noqa: E402
from src.mcd_client import (McdClient, McpError,          # noqa: E402
                            READ_ONLY, FREE_WRITE, MONEY_WRITE)
from src import (price_memory, perceive, optimize,     # noqa: E402
                 coupons as coupons_mod, order_exec, persona, payloads,
                 present, ledger, share_card, address, onboarding, local_data)

FORMATS = ("inline", "json", "html", "svg")


def _configure_output_encoding() -> None:
    """让 CLI 被直接导入调用时也不会因 Windows 代码页无法编码 emoji 而崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


_configure_output_encoding()


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


def _patrol_output_path(out: str, payload: dict, fmt: str,
                        index: int, total: int) -> str:
    """为巡检多张 HTML/SVG 卡生成稳定且不覆盖的文件名。"""
    if fmt not in ("html", "svg") or total <= 1:
        return out or ""
    if not out:
        return str(Path("data") / f"{payload['type']}-{fmt}-{index}.{fmt}")
    path = Path(out)
    suffix = path.suffix or f".{fmt}"
    stem = path.stem if path.suffix else path.name
    return str(path.with_name(f"{stem}-{index}{suffix}"))


# ---------------------------------------------------------------------------
def cmd_schedule(args):
    """G 定时执行：登记 / 查看 / 到点触发（只推送确认消息，不自动下单）。"""
    from src import schedule as sch

    act = args.action
    if act == "add":
        try:
            created = sch.add(kind=args.kind, at=args.at, store=args.store or "",
                              items=args.items or "", track=args.track,
                              coupon=args.coupon, note=args.note, days=args.days)
        except ValueError as e:
            print(f"[schedule] {e}")
            sys.exit(1)
        print(f"✅ 已登记：{sch.describe(created)}")
        print("⛔ 到点只会给你推确认消息，绝不会自动下单。")
        print("（到点确认后按**当下价格**下即时单，不做预约单；--at 是触发提醒时间）")
        print("（到点触发：python main.py schedule due --fire）")
        return

    if act == "list":
        items = sch.list_all(only_pending=args.pending)
        if not items:
            print("还没有定时任务。例：python main.py schedule add --at 18:30 "
                  "--store 1421252 --items \"麦旋风 x1\" --note \"加班餐\"")
            return
        print(f"共 {len(items)} 条：")
        for s in items:
            print("  " + sch.describe(s))
        return

    if act in ("rm", "cancel"):
        ok = sch.remove(args.id) if act == "rm" else sch.cancel(args.id)
        print(("已删除" if ok else "未找到该任务") + f"：{args.id}")
        return

    if act == "due":
        pend = sch.due()
        if not pend:
            print("暂无到点任务。（提示：schedule watch 可常驻轮询）")
            return
        print(f"⏰ 到点任务 {len(pend)} 条：")
        for s in pend:
            print("  " + sch.describe(s))
        if not args.fire:
            print("加 --fire 才会重新算价并推送确认消息（仍不会自动下单）。")
            return
        client = _client()
        for s in pend:
            try:
                r = sch.fire(client, s)
            except Exception as e:
                print(f"  ❌ {s['id']} 触发失败：{e}")
                continue
            if r.get("skipped"):
                print(f"  ⏭️ {s['id']} 已跳过：{r.get('reason') or '任务状态已变化'}")
                continue
            print()
            print(f"--- 推送内容（{s['id']}）---")
            print(present.render_inline(r["payload"]))
            print(f"确认卡已落盘：{r['html']}")
            if r["draft"]:
                print(f"要下单请回复「确认下单」，执行："
                      f'python main.py order --draft "{r["draft"]}" --yes --confirm-text "确认下单"')
        return

    if act == "watch":
        import time as _t
        print(f"轮询中：每 {args.interval}s 检查一次到点任务（Ctrl+C 退出）")
        print("提醒：生产环境建议用系统计划任务/WorkBuddy 定时任务调 schedule due --fire")
        try:
            while True:
                if sch.due():
                    cmd_schedule(argparse.Namespace(
                        action="due", fire=True, pending=False, id=None, interval=None))
                _t.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n已停止轮询。")


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
        required = []
        command = hit["cmd"]
        if "<门店code>" in command and not args.store:
            required.append("--store 门店code")
        if "<商品>" in command and not args.item:
            required.append("--item 商品名")
        if "<地址id>" in command and not getattr(args, "address_id", ""):
            required.append("--address-id 配送地址id")
        if required:
            print("执行前还需要你提供：" + "、".join(required))
            return
        print("→ 立即执行：")
        import subprocess
        argv = ph.build_argv(hit["cmd"], store=args.store, item=args.item,
                             draft=args.draft,
                             address_id=getattr(args, "address_id", ""))
        completed = subprocess.run(argv)  # 无 shell
        if completed.returncode:
            sys.exit(completed.returncode)


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
    if getattr(args, "format", "text") == "json":
        _print(res)      # json 模式：stdout 只输出一个 JSON 对象（text 已含在 res 里）
    else:
        print(res["text"])
    sys.exit(0 if res["ready"] else 1)


def cmd_capabilities(args):
    """Agent 自述元信息：我是谁 / 我会什么 / 我绝不做什么。"""
    if getattr(args, "format", "text") == "json":
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
    store_results = report.get("scannedStores") or []
    scanned = sum(1 for store in store_results if not store.get("error"))
    scan_errors = sum(1 for store in store_results if store.get("error"))
    payloads_out = [payloads.alert_price_drop(
        report["priceEvents"], nick, scanned, scan_errors)]
    ep = payloads.alert_expiring_points(report.get("expirePoints"), nick)
    if ep:
        payloads_out.append(ep)
    avail = [c for c in (report.get("newCouponsAvailable") or [])
             if coupons_mod.is_claimable(c)]
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
    total = len(payloads_out)
    for index, p in enumerate(payloads_out, 1):
        _show(p, args, out=_patrol_output_path(
            getattr(args, "out", ""), p, args.format, index, total))
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


def cmd_delivery(args):
    """查配送地址 + 可配送门店（只读，外卖下单的前置步骤）。"""
    client = _client()
    if not args.address_id:
        try:
            raw = client.delivery_addresses()
        except McpError as e:
            print(f"[delivery] 查地址失败: {e}")
            sys.exit(1)
        d = raw.get("data") if isinstance(raw, dict) else raw
        addrs = (d or {}).get("addresses") or [] if isinstance(d, dict) else []
        if not addrs:
            print("没有已保存的配送地址（可在麦当劳 App「麦乐送」里先存一个）。")
            return
        print(f"你有 {len(addrs)} 个配送地址：")
        for a in addrs:
            print(f"  [{a.get('addressId')}] {a.get('fullAddress')}"
                  f"（{a.get('contactName')} {a.get('phone')}）")
        print()
        print("下一步查可配送门店：python main.py delivery --address-id <id>")
        print("或直接算外卖价：python main.py best --delivery --address-id <id> --items \"麦乐鸡5块 x1\"")
        return

    try:
        raw = client.delivery_stores(args.address_id)
    except McpError as e:
        print(f"[delivery] 查可配送门店失败: {e}")
        sys.exit(1)
    d = raw.get("data") if isinstance(raw, dict) else raw
    stores = d if isinstance(d, list) else (d or {}).get("list") or []
    if not stores:
        print("该地址暂无营业中的可配送门店。")
        return
    print(f"该地址可配送到 {len(stores)} 家门店：")
    for s in stores:
        status = "营业中" if optimize._store_open(s) else "休息中"
        print(f"  {s.get('storeName')}  storeCode={s.get('storeCode')}"
              f"  beCode={s.get('beCode')}  [{status}]")
    print()
    print("下一步算外卖价：python main.py best --delivery --address-id "
          f"{args.address_id} --store <storeCode> --items \"...\"")


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
        claimable = [c for c in items if coupons_mod.is_claimable(c)]
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
    """D 最优下单 → confirm payload（确认闸交互卡片）。到店自提或麦乐送外卖。"""
    wanted = []
    try:
        for part in args.items.split(","):
            p = part.strip()
            if not p:
                continue
            if "x" in p.lower():
                name, qty = p.lower().split("x", 1)
                qty = int(qty.strip())
                if not name.strip() or qty <= 0:
                    raise ValueError
                wanted.append((name.strip(), qty))
            else:
                wanted.append((p, 1))
    except ValueError:
        print("商品格式无效：请使用“商品名 x正整数”，例如“麦旋风 x1”")
        sys.exit(1)
    if not wanted:
        print("至少需要指定一个商品")
        sys.exit(1)
    client = _client()
    store_code = args.store
    delivery = None
    if args.delivery:
        if not args.address_id:
            print("外卖需要配送地址：先 python main.py delivery 查地址，再 --address-id <id>")
            sys.exit(1)
        if args.be_code and not args.store:
            print("外卖单独指定 --be-code 时必须同时指定 --store，以校验业务码归属")
            sys.exit(1)
        try:
            # --store 与 --be-code 同时指定时也必须过校验：
            # resolve_delivery 会核对「门店确实配送该地址 + 业务码与接口一致」，
            # 避免把无效的 (storeCode, beCode) 组合直接送给算价接口
            resolved = optimize.resolve_delivery(
                client, args.address_id,
                store_code=(args.store or None),
                be_code=args.be_code or None)
        except (ValueError, McpError) as e:
            print(f"[best] 外卖解析失败: {e}")
            sys.exit(1)
        store_code = args.store or resolved["storeCode"]
        delivery = {"addressId": args.address_id,
                    "beCode": args.be_code or resolved["beCode"],
                    "addressText": resolved["addressText"]}
    elif not store_code:
        print("到店下单需要门店：--store <storeCode>（外卖用 --delivery --address-id <id>）")
        sys.exit(1)
    extra_draft_path = ""
    try:
        if args.track == "dual":
            # 双轨候选：同时算「全局最低」+「常买Top3」，出双候选确认卡二选一。
            # top3 轨道失败不拖垮 global（降级为单候选确认卡并附原因）。
            dual = optimize.optimize_dual(client, store_code, wanted, delivery=delivery)
            if dual["top3"] is None:
                draft = dual["global"]
                if dual["warnings"]:
                    draft.setdefault("pricingWarnings", []).extend(dual["warnings"])
                path = order_exec.save_draft(draft)
                confirm_payload = payloads.confirm_from_draft(draft, str(path))
            else:
                paths = {"global": str(order_exec.save_draft(dual["global"])),
                         "top3": str(order_exec.save_draft(dual["top3"]))}
                path = Path(paths["global"])
                draft = dual["global"]
                extra_draft_path = paths["top3"]
                confirm_payload = payloads.confirm_candidates(
                    dual["global"], dual["top3"],
                    path_a=paths["global"], path_b=paths["top3"])
                if dual["warnings"]:
                    confirm_payload["meta"]["hint"] += "｜⚠️ " + "；".join(dual["warnings"])
        else:
            draft = optimize.optimize(client, store_code, wanted, track=args.track,
                                      delivery=delivery)
            path = order_exec.save_draft(draft)
            confirm_payload = payloads.confirm_from_draft(draft, str(path))
    except (ValueError, McpError) as e:
        print(f"[best] 失败: {e}")
        sys.exit(1)
    if args.format == "json":
        confirm_payload["draftPath"] = str(path)  # 草稿路径进 JSON，供脚本/宿主取用
        if extra_draft_path:
            confirm_payload["top3DraftPath"] = extra_draft_path
        text = json.dumps(confirm_payload, ensure_ascii=False, indent=2)
        envelope = {
            "payload": confirm_payload,
            "saved": bool(args.out),
            "payloadPath": None,
            "draftPath": str(path),
            "top3DraftPath": extra_draft_path or None,
        }
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            envelope["payloadPath"] = str(out.resolve())
        # 无论是否 --out，stdout 都是同一套 envelope，避免脚本调用者分支解析。
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
        return
    _show(confirm_payload, args, out=args.out)
    if not (args.out and args.format in ("html", "svg")):
        print(f"\n订单草稿已保存: {path}")
        if extra_draft_path:
            print(f"双轨另一候选草稿: {extra_draft_path}")
        print(optimize.draft_to_json(draft))


def cmd_order(args):
    """下单（红线闸）：无 --yes 一律拒绝；确认必须绑定「刚展示的那份草稿」。"""
    draft_path = Path(order_exec.DRAFT_DIR) / args.draft
    if not draft_path.exists():
        draft_path = Path(args.draft)
    draft = order_exec.load_draft(draft_path)
    if not args.yes:
        print(persona.blocked_by_gate())
        print(optimize.draft_to_json(draft))
        sys.exit(4)
    # ① 确认凭据必须精确匹配（一次性确认凭证，非任意非空字符串）
    if str(args.confirm_text or "").strip() != order_exec.CONFIRM_TEXT:
        print(f'需要精确回复「{order_exec.CONFIRM_TEXT}」作为确认凭据：'
              f'--confirm-text "{order_exec.CONFIRM_TEXT}"')
        sys.exit(4)
    # ② 内容摘要 + 报价时效 + 券未过期
    ok, reason = order_exec.validate_for_order(draft)
    if not ok:
        print(f"[下单被拦截] {reason}")
        print("请重新执行：python main.py best --store <门店code> --items \"<商品> x1\"")
        sys.exit(4)
    # ③ 重新算价并比对：价格/券/商品有变化即拒绝（绑定「刚展示的那份草稿」）
    client = _client()
    try:
        fresh = optimize.reprice_draft(client, draft)
    except (ValueError, McpError) as e:
        print(f"[下单被拦截] 重新算价失败，拒绝下单：{e}")
        sys.exit(4)
    changes = order_exec.diff_draft(draft, fresh)
    if changes:
        print("⚠️ 草稿报价已过期，以下内容已变化，请重新生成草稿并确认：")
        for c in changes:
            print(f"  - {c}")
        print("重新算价：python main.py best --store <门店code> --items \"<商品> x1\"")
        sys.exit(4)
    # ④ 写入确认标记并执行
    draft["confirmedByHuman"] = True
    draft["confirmedAt"] = sys_date()
    draft["confirmedText"] = args.confirm_text
    if not order_exec.human_gate(draft):
        print(persona.blocked_by_gate())
        sys.exit(4)
    receipt = order_exec.execute_draft(client, draft)
    if receipt.get("status") == "uncertain":
        print("⚠️ 下单结果不确定（网络层错误），未入账本，也请不要重复执行下单：")
        if not receipt.get("receiptLogRecorded", True):
            print("本地回执日志写入失败；请先到麦当劳 App 核对订单状态。")
        _print(receipt)
        sys.exit(2)
    # 到点任务下单成功 → 置 done（与「等用户确认后置 done」的状态说明一致）
    sched = draft.get("scheduledFrom")
    if sched and sched.get("id"):
        from src import schedule as sch
        sch.complete(sched["id"])
    verification = receipt.get("savingsVerification", {})
    if verification.get("verified") and verification.get("ledgerRecorded"):
        print("订单详情已核实；服务端优惠金额已计入「实际省下」。")
    elif verification.get("verified"):
        print("订单详情已核实，但本地账本写入失败；本次优惠未计入账单，请勿因此重复下单。")
    else:
        print("订单已创建，但优惠金额未能从订单详情核实；报价金额未计入「实际省下」。")
    if not receipt.get("receiptLogRecorded", True):
        print("下单回执日志未能写入本地文件。")
    _print(receipt)


def cmd_cancel(args):
    """取消近期可见的未支付订单（MONEY_WRITE，需 --yes --confirm-text 确认）。"""
    from src import cancel as cancel_mod
    client = _client()
    unpaid, all_orders = cancel_mod.list_unpaid_orders(client)
    if not unpaid:
        print(f"近期可见的 {len(all_orders)} 笔订单中，没有发现未支付订单（均为已完结状态）。")
        print("（接口仅返回近期订单，更早的未支付单请到 App 查看。）")
        return
    if not args.yes:
        _show(payloads.cancel_unpaid_preview(unpaid, get_config().nickname, len(all_orders)), args)
        return
    if str(args.confirm_text or "").strip() != cancel_mod.CONFIRM_TEXT:
        print(f'需要精确回复「{cancel_mod.CONFIRM_TEXT}」作为确认凭据：'
              f'--confirm-text "{cancel_mod.CONFIRM_TEXT}"')
        sys.exit(4)
    receipt = cancel_mod.cancel_orders(client, unpaid, args.reason)
    _show(payloads.cancel_unpaid_receipt(receipt, get_config().nickname), args)


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


def cmd_data(args):
    """管理本地缓存、配置副本和生成文件。"""
    if args.action == "status":
        result = local_data.status()
        if args.format == "json":
            _print(result)
            return
        labels = {"cache": "缓存与历史", "config": "本地配置", "generated": "生成文件"}
        for group in result["groups"]:
            print(f"{labels[group['name']]}: {group['files']} 个文件，{group['bytes']} 字节")
            print(f"  路径: {group['path']}")
            if group.get("unmanagedFiles"):
                print(f"  未登记文件（导出/删除不处理）: {group['unmanagedFiles']} 个，"
                      f"{group['unmanagedBytes']} 字节")
            if group["skippedSymlinks"]:
                print(f"  已跳过符号链接: {group['skippedSymlinks']} 项")
        print(f"合计: {result['totalFiles']} 个文件，{result['totalBytes']} 字节")
        return

    if args.action in {"export", "redact"}:
        redact = args.action == "redact" or args.redact
        try:
            result = local_data.export_archive(args.out, redact=redact, overwrite=args.force)
        except (ValueError, OSError) as e:
            print(f"[data] 导出失败：{e}", file=sys.stderr)
            sys.exit(1)
        if args.format == "json":
            _print(result)
        else:
            mode = "已脱敏副本" if redact else "Token 已剔除的备份"
            print(f"{mode}已导出：{result['path']}（{result['files']} 个文件，{result['bytes']} 字节）")
            print("原始本地文件未修改。" if redact else "订单与地址等个人数据仍可能包含在备份中。")
        return

    if not args.yes or args.confirm_text.strip() != local_data.DELETE_CONFIRM_TEXT:
        result = local_data.status()
        print(f"将删除缓存与历史数据（{result['groups'][0]['managedFiles']} 个已登记文件）。")
        print("其它目录默认保留；加 --include-config / --include-generated 才会删除。")
        scopes = (" --include-config" if args.include_config else "")
        scopes += (" --include-generated" if args.include_generated else "")
        print(f'确认后重跑：python main.py data delete{scopes} --yes --confirm-text "删除本地数据"')
        sys.exit(4)
    try:
        result = local_data.delete(include_config=args.include_config,
                                   include_generated=args.include_generated)
    except OSError as e:
        print(f"[data] 删除未完成：{e}。请运行 data status 检查剩余文件。", file=sys.stderr)
        sys.exit(1)
    if args.format == "json":
        _print(result)
    else:
        print(f"已删除 {result['deletedFiles']} 个本地文件。")
        for group, count in result["byGroup"].items():
            print(f"  {group}: {count} 个")


def cmd_share(args):
    """I 下单前优惠方案 → share payload（图片，对外转发）。"""
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


def _raw_tool_gate(tool: str) -> str | None:
    """raw 调试入口的工具放行判定：返回拒绝原因，None = 放行。

    红线：raw 只是调试通道，绝不能借它绕过「写动作需人工确认」的红线，
    因此 FREE_WRITE（领券/建地址）与 MONEY_WRITE（下单/取消）一律拦截，
    只放行 READ_ONLY。
    """
    if tool in MONEY_WRITE:
        return f"⛔ {tool} 是花钱/花积分动作，raw 调试入口只放行只读工具。"
    if tool in FREE_WRITE:
        return f"⛔ {tool} 是免费写动作（会改动账户，如领券/建地址），raw 只放行只读工具。"
    if tool not in READ_ONLY:
        return f"⛔ {tool} 不在只读白名单中，raw 拒绝调用。"
    return None


def cmd_raw(args):
    """只读调试：仅放行 READ_ONLY 白名单工具，绝不放行写动作。"""
    deny = _raw_tool_gate(args.tool)
    if deny:
        print(deny)
        sys.exit(1)
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

    def add_text_fmt(p, default="text"):
        p.add_argument("--format", choices=("text", "json"), default=default,
                       help="输出形态：text 文本(默认) / json 结构化")

    p = sub.add_parser("init", help="生成 config.local.json 模板 + 领 Token 指引")
    p.add_argument("--force", action="store_true", help="覆盖已存在的配置")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("phrase", help="常用话术 → 命令映射（不传参数则打印话术表）")
    p.add_argument("text", nargs="?", default="", help="用户说的一句话，如「麦当劳有什么优惠」")
    p.add_argument("--run", action="store_true", help="只读意图可直接执行；花钱意图一律拒绝")
    p.add_argument("--store", default="", help="配合 --run 补门店 code")
    p.add_argument("--item", default="", help="配合 --run 补商品名")
    p.add_argument("--draft", default="", help="配合 --run 补草稿文件名")
    p.add_argument("--address-id", default="", help="配合 --run 补外卖配送地址 addressId")
    p.set_defaults(fn=cmd_phrase)

    # ---------- G 定时执行 ----------
    sp = sub.add_parser("schedule", help="定时下单/到点提醒（到点只推确认消息，绝不自动下单）")
    ssub = sp.add_subparsers(dest="action", required=True)

    a = ssub.add_parser("add", help="登记定时任务")
    a.add_argument("--at", required=True, help='"18:30" 或 "2026-10-13 18:30"')
    a.add_argument("--kind", choices=["order", "alert"], default="order")
    a.add_argument("--store", default="", help="门店 storeCode")
    a.add_argument("--items", default="", help='"麦旋风 x1,薯条 x1"')
    a.add_argument("--track", choices=["global", "top3"], default="global",
                   help="global=全菜单价格最优；top3=购买量Top3里挑（定时任务单轨）")
    a.add_argument("--coupon", default="auto",
                   help="auto=到点重算最优券；也可填具体券 id/code（到点只测该券，"
                        "不可用时按无券价并注明生效时间）")
    a.add_argument("--note", default="", help="备注，如「加班餐」")
    a.add_argument("--days", type=int, default=0, help="连续重复天数，0=仅一次")
    a.add_argument("--at-alert", dest="at_alert", default="", help="alert 类型的提醒文案")

    a = ssub.add_parser("list", help="列出所有定时任务")
    a.add_argument("--pending", action="store_true", help="只看未触发")

    a = ssub.add_parser("rm", help="删除任务")
    a.add_argument("id")
    a = ssub.add_parser("cancel", help="取消任务（保留记录）")
    a.add_argument("id")

    a = ssub.add_parser("due", help="检查到点任务")
    a.add_argument("--fire", action="store_true", help="到点则重算价并推送确认消息（不下单）")

    a = ssub.add_parser("watch", help="常驻轮询到点任务（Ctrl+C 退出）")
    a.add_argument("--interval", type=int, default=60, help="轮询间隔秒")

    sp.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("doctor", help="首次使用自检（只读，不下单不领券）")
    p.add_argument("--offline", action="store_true", help="跳过 Token 连通性探测（离线可用）")
    add_text_fmt(p)
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("capabilities", help="能力清单与红线（给 Agent 读的自述元信息）")
    add_text_fmt(p)
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

    p = sub.add_parser("delivery", help="查配送地址 + 可配送门店（只读，外卖前置）")
    p.add_argument("--address-id", default="", help="配送地址 ID；缺省则列出所有配送地址")
    p.set_defaults(fn=cmd_delivery)

    p = sub.add_parser("best", help="算最优价 → confirm 确认卡（只读；到店自提或 --delivery 麦乐送）")
    p.add_argument("--store", default="", help="门店 storeCode（到店必填；外卖可省略自动选可配送门店）")
    p.add_argument("--items", required=True, help='"商品名或code x数量,..."')
    p.add_argument("--track", choices=["global", "top3", "dual"], default="global",
                   help="global=全菜单价格最优(默认)；top3=购买量Top3；"
                        "dual=双轨候选二选一（全局最低 vs 常买Top3）")
    p.add_argument("--delivery", action="store_true", help="麦乐送外卖（需 --address-id）")
    p.add_argument("--address-id", default="", help="配送地址 ID（python main.py delivery 查）")
    p.add_argument("--be-code", default="", help="业务编码（可选；缺省自动从可配送门店解析）")
    add_fmt(p)
    p.set_defaults(fn=cmd_best)

    p = sub.add_parser("order", help="下单（红线：需 --yes --confirm-text）")
    p.add_argument("--draft", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--confirm-text", dest="confirm_text", default="")
    p.set_defaults(fn=cmd_order)

    p = sub.add_parser("cancel", help="取消近期可见的未支付订单（红线：需 --yes --confirm-text）")
    p.add_argument("--yes", action="store_true", help="确认执行批量取消")
    p.add_argument("--confirm-text", dest="confirm_text", default="",
                   help="确认凭据，须精确等于「确认取消」")
    p.add_argument("--reason", default="-1",
                   help="取消原因 code：1=改主意 2=重复下单 3=点错 4=地址/电话错 5=送达时间错 -1=其它")
    add_fmt(p)
    p.set_defaults(fn=cmd_cancel)

    p = sub.add_parser("report", help="省钱账单 → report HTML 页面")
    p.add_argument("--period", choices=["today", "week", "month", "all"], default="month")
    add_fmt(p, default="html")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("data", help="查看、导出、脱敏或删除本地数据")
    dsub = p.add_subparsers(dest="action", required=True)
    d = dsub.add_parser("status", help="查看本地数据文件数量与占用")
    d.add_argument("--format", choices=["text", "json"], default="text")
    d.set_defaults(fn=cmd_data)
    d = dsub.add_parser("export", help="导出本地数据；始终剔除 Token")
    d.add_argument("--out", required=True, help="输出 ZIP 文件路径")
    d.add_argument("--redact", action="store_true", help="同时脱敏结构化个人信息")
    d.add_argument("--force", action="store_true", help="覆盖已存在的导出文件")
    d.add_argument("--format", choices=["text", "json"], default="text")
    d.set_defaults(fn=cmd_data)
    d = dsub.add_parser("redact", help="导出脱敏副本，不修改原文件")
    d.add_argument("--out", required=True, help="输出 ZIP 文件路径")
    d.add_argument("--force", action="store_true", help="覆盖已存在的导出文件")
    d.add_argument("--format", choices=["text", "json"], default="text")
    d.set_defaults(fn=cmd_data)
    d = dsub.add_parser("delete", help="删除本地缓存；其它范围须显式指定")
    d.add_argument("--yes", action="store_true", help="确认删除")
    d.add_argument("--confirm-text", default="", help="须精确填写「删除本地数据」")
    d.add_argument("--include-config", action="store_true", help="同时删除含 Token/个人信息的 config.local.json")
    d.add_argument("--include-generated", action="store_true", help="同时删除 data/ 下的生成文件")
    d.add_argument("--format", choices=["text", "json"], default="text")
    d.set_defaults(fn=cmd_data)

    p = sub.add_parser("share", help="待下单优惠方案 → share 图片")
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
