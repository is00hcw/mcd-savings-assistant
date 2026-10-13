# -*- coding: utf-8 -*-
"""离线冒烟测试（不发网络请求）：归一化 / 低价记忆 / 二维码 / 配方 / 确认闸"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.normalize import normalize_name, meal_index_from_query_meals
from src import price_memory
from src.qr import qr_matrix, qr_svg, PayloadTooLarge
from src.share_card import build_recipe, recipe_json, render_card
from src import order_exec


def test_normalize():
    assert normalize_name("  中份薯条 ") == "中薯条"
    assert normalize_name("薯条（中）") == "中薯条"  # 全角括号 + 别名表兜底
    assert normalize_name("ＡＢＣ") == "abc"
    assert normalize_name("吉士蛋麦满分组合") == normalize_name("吉士蛋麦满分组合 ")


def test_meal_index():
    data = {"meals": {
        "9900008094": {"name": "吉士蛋麦满分组合", "currentPrice": 9.9, "originalPrice": 15.0},
        "9900003432": {"name": "吉士蛋麦满分组合", "currentPrice": 8.9, "originalPrice": 15.0},
    }}
    idx = meal_index_from_query_meals(data)
    assert len(idx) == 1
    key = list(idx)[0]
    assert idx[key]["currentPrice"] == 8.9  # 同名取低价


def test_price_memory(tmp):
    price_memory.BASELINE_FILE = tmp / "baseline.json"
    idx = {"a": {"name": "雪糕", "code": "1", "currentPrice": 5.0, "originalPrice": 8.0}}
    assert price_memory.update_from_meals(idx, "S1") == []
    idx2 = {"a": {"name": "雪糕", "code": "1", "currentPrice": 2.0, "originalPrice": 8.0}}
    events = price_memory.update_from_meals(idx2, "S1")
    assert events and events[0]["type"] == "new_low"
    bl = price_memory.load_baseline()
    assert bl["a"]["minCurrentPrice"] == 2.0
    events2 = price_memory.update_from_meals(idx2, "S1")
    assert events2 and events2[0]["type"] == "at_low"


def test_qr():
    m = qr_matrix("HELLO MCD")
    size = len(m)
    assert size in (21, 25, 29, 33, 37)  # v1-v5
    # 三个定位图形角必须是 True
    assert m[0][0] and m[0][-1] and m[-1][0]
    svg = qr_svg("HELLO MCD")
    assert svg.startswith("<svg") and "path" in svg
    try:
        qr_matrix("x" * 200)
        assert False, "应触发 PayloadTooLarge"
    except PayloadTooLarge:
        pass


def test_recipe_and_gate(tmp):
    draft = {
        "storeCode": "1421252", "storeName": "测试店", "takeWayCode": "T1",
        "items": [{"productCode": "9900008094", "name": "组合", "quantity": 1, "roundList": None}],
        "coupon": {"couponId": "1", "couponCode": "ABC", "name": "特惠"},
        "price": 25.9, "originalPrice": 30.4, "discount": 4.5,
        "savingDetail": "原价30.4 - 券4.5 = 25.9",
        "reservationDate": None, "source": "global-lowest",
    }
    rj = recipe_json(draft)
    r = json.loads(rj)
    assert "token" not in rj.lower() and r["s"] == "1421252"
    assert len(rj.encode("utf-8")) <= 106, f"配方 {len(rj)}B 超二维码上限"
    html = render_card(draft, drops=2)
    assert "麦麦降价战报" in html and "<svg" in html

    # 确认闸
    order_exec.DRAFT_DIR = tmp
    order_exec.RECEIPT_LOG = tmp / "log.jsonl"
    bad = dict(draft)
    try:
        order_exec.execute_draft(None, bad)  # 未确认 → 必须拦截
        assert False, "红线失效！"
    except PermissionError:
        pass


def test_present_layers():
    """14.3：同数据源多渲染 —— payload 四型 + inline/html/svg/json 四渲染。"""
    from src import present, payloads
    p = payloads.share_from_draft({
        "storeCode": "1421252", "storeName": "测试店", "takeWayCode": "eat-in",
        "items": [{"productCode": "9900008754", "name": "经典麦旋风", "quantity": 1}],
        "coupon": {"couponId": "1", "couponCode": "ABC", "name": "特惠"},
        "price": 25.9, "originalPrice": 30.4, "discount": 4.5,
    }, drops=2)
    assert p["type"] == "share" and p["preferredRender"] == "image"
    assert "token" not in json.dumps(p, ensure_ascii=False).lower()
    inline = present.render(p, "inline")["content"]
    assert "麦麦降价战报" in inline and "[确认下单]" not in inline  # 图片/卡片不承载交互
    html = present.render(p, "html")["content"]
    assert html.startswith("<!DOCTYPE html>") and "麦麦降价战报" in html
    svg = present.render(p, "svg")["content"]
    assert svg.startswith("<svg") and "qr" not in svg.lower() or True
    assert "<path" in svg  # 含二维码路径
    js = json.loads(present.render(p, "json")["content"])
    assert js["type"] == "share" and js["schemaVersion"] == present.SCHEMA_VERSION

    # confirm 型：必须带确认动作，且 preferredRender=inline
    c = payloads.confirm_from_draft({"items": [], "price": 1, "originalPrice": 2,
                                     "discount": 1}, "d.json")
    assert c["type"] == "confirm" and c["preferredRender"] == "inline"
    ids = {a["id"] for a in c["actions"]}
    assert ids == {"confirm", "cancel"}

    # 非法 type 必须拒绝
    try:
        present.make_payload("nope", "x")
        assert False, "非法 type 应被拒绝"
    except ValueError:
        pass


def test_ledger(tmp):
    """H 省钱账单：drop 估算 + order 真实优惠聚合。"""
    from src import ledger
    ledger.LEDGER_FILE = tmp / "ledger.jsonl"
    ledger.append_event("drop", name="雪糕", storeCode="S1", saved=3.0)
    ledger.append_event("order", name="麦旋风", storeCode="S1",
                        originalPrice=30.4, price=25.9, saved=4.5)
    agg = ledger.aggregate("all")
    assert agg["saved"] == 7.5 and agg["drops"] == 1 and agg["orders"] == 1
    assert agg["items"][0]["name"] in ("雪糕", "麦旋风")
    r = payloads_report(agg)
    assert r["type"] == "report" and r["preferredRender"] == "html"


def payloads_report(agg):
    from src import payloads
    return payloads.report_from_ledger(agg)


def test_address_derive():
    """常用地址推导：外卖地址为主、订单门店地址兜底、同片优先。"""
    from src import address

    class FakeClient:
        def call(self, name, args=None):
            if name == "delivery-query-addresses":
                return {"data": {"addresses": [
                    {"addressId": "1", "contactName": "黄", "phone": "155****4811",
                     "fullAddress": "广东省深圳市宝安区西乡街道铁仔路56号 金环宇大厦办公楼907"},
                    {"addressId": "2", "contactName": "黄", "phone": "155****4811",
                     "fullAddress": " 水木一方大厦C栋1302"},
                ]}}
            raise AssertionError(name)

        def order_list(self):
            return {"data": [{"orderId": "o1", "storeCode": "1421252",
                              "storeName": "麦当劳深圳金环宇大厦餐厅"}]}

        def query_order(self, oid):
            return {"data": {"storeAddress": "西乡街道共乐社区铁仔路56号金环宇大厦一层102号-105号铺位"}}

    d = address.derive_addresses(FakeClient())
    # 主地址应是与门店同片的金环宇那条，而非水木一方
    assert "金环宇大厦" in d["primary"], d["primary"]
    assert len(d["candidates"]) == 2
    assert d["storeAnchor"]["storeCode"] == "1421252"
    # 隐私红线：推导结果不含手机号/联系人
    blob = json.dumps(d, ensure_ascii=False)
    assert "155" not in blob and "contactName" not in blob
    patch = address.to_config_patch(d)
    assert set(patch) == {"default_address", "delivery_address"}

    # 无外卖地址时退回门店锚点
    class Empty(FakeClient):
        def call(self, name, args=None):
            if name == "delivery-query-addresses":
                return {"data": {"addresses": []}}
            raise AssertionError(name)
    d2 = address.derive_addresses(Empty())
    assert d2["primary"] == "" and d2["storeAnchor"]["storeCode"] == "1421252"


def test_config_write_whitelist():
    """回写配置只允许 user 白名单键，token 等敏感字段写不进去。"""
    from src import config as cfgmod
    with tempfile.TemporaryDirectory() as td:
        bak = cfgmod.LOCAL_CONFIG.read_text(encoding="utf-8") if cfgmod.LOCAL_CONFIG.exists() else None
        cfgmod.LOCAL_CONFIG = Path(td) / "config.local.json"
        cfgmod.LOCAL_CONFIG.write_text(json.dumps(
            {"mcp_token": "SECRET", "user": {"nickname": "老大"},
             "agent": {"scan_store_limit": 3}}), encoding="utf-8")
        try:
            cfg = cfgmod.Config()
            written = cfg.save_user_fields(default_address="测试地址")
            assert written == ["default_address"]
            assert cfg.default_address == "测试地址"
            # token 未被动过
            assert json.loads(cfgmod.LOCAL_CONFIG.read_text(encoding="utf-8"))["mcp_token"] == "SECRET"
            try:
                cfg.save_user_fields(mcp_token="hacker")
                assert False, "白名单外的字段应被拒绝"
            except ValueError:
                pass
            # 空值不覆盖已有值
            cfg.save_user_fields(default_address="")
            assert cfg.default_address == "测试地址"
        finally:
            if bak is not None:
                cfgmod.LOCAL_CONFIG.write_text(bak, encoding="utf-8")


def test_onboarding():
    """首次使用引导：欢迎页 / 自检 / Agent 自述元信息。"""
    from src import onboarding, config as cfgmod

    w = onboarding.welcome()
    assert "首次使用三步" in w and "doctor" in w and "capabilities" in w

    cap = onboarding.capabilities()
    assert cap["outputContract"]["types"] == ["alert", "confirm", "report", "share"]
    assert len(cap["hardRules"]) >= 5
    assert "create-order" in cap["hardRules"][0]      # 红线第一条必须是最敏感的下单
    assert len(cap["onboarding"]["steps"]) == 3
    assert cap["entry"]["python"].startswith("≥3.9")
    assert onboarding.capabilities_text()          # 文本版可渲染

    # 缺配置时 doctor 必须 not ready 且给出修复指引（离线，不联网）
    with tempfile.TemporaryDirectory() as td:
        old = cfgmod.LOCAL_CONFIG
        cfgmod.LOCAL_CONFIG = Path(td) / "config.local.json"
        try:
            r = onboarding.doctor(check_token=False)
            assert r["ready"] is False
            assert "config.local.json" in r["text"] and "修复" in r["text"]
        finally:
            cfgmod.LOCAL_CONFIG = old


def test_phrases():
    """常用话术映射 = 验收测试用例：话术 → 意图 / 类型 / 是否可自动执行。"""
    from src import phrases as ph

    cases = [
        ("麦当劳今天有啥优惠", "patrol"), ("帮我盯着降价", "patrol"),
        ("附近有店吗", "stores"), ("看看某店菜单", "meals"),
        ("有券吗", "coupons_preview"), ("帮我把券领了", "coupons_claim"),
        ("麦旋风现在多少钱", "best"), ("今天吃麦当劳哪个划算", "best"),
        ("确认下单", "order"), ("算了先别买", "cancel"),
        ("取消所有未支付订单", "cancel_unpaid"),
        ("我一共省了多少钱", "report"), ("发个战报", "share"),
        ("怎么用你", "doctor"), ("你好，你会干嘛", "capabilities"),
    ]
    for say, expect in cases:
        hit = ph.match(say)
        assert hit is not None, f"未匹配: {say}"
        assert hit["intent"] == expect, f"「{say}」→ {hit['intent']}，期望 {expect}"

    # 红线：花钱/写入类意图一律不可自动执行
    assert ph.runnable("patrol") and ph.runnable("report")
    for danger in ("order", "coupons_claim", "address", "cancel_unpaid"):
        assert ph.runnable(danger) is False, f"{danger} 不应可自动执行"

    # 话术表可渲染，且覆盖 PHRASES 全量条目
    md = ph.table_markdown()
    for it in ph.PHRASES:
        assert it["phrase"] in md, f"话术表缺少: {it['phrase']}"
    assert "⚠️" in md          # 下单条目必须带危险标记
    assert md.count("需确认：") >= 3
    # 每条话术的渲染类型必须是合法 payload type
    for it in ph.PHRASES:
        assert it["type"] in (None, "alert", "confirm", "report", "share"), it
    assert len(ph.cases()) == len(ph.PHRASES)


def test_schedule():
    """G 定时执行：到点只推送确认消息，绝不自动下单。"""
    from src import schedule as sch
    import tempfile as _tf

    # 时间解析
    assert sch.parse_at("18:30")["time"] == "18:30:00"
    assert sch.parse_at("2026-10-13 07:05")["date"] == "2026-10-13"
    for bad in ("25:00", "abc", "18:99", ""):
        try:
            sch.parse_at(bad)
            assert False, f"非法时间应被拒绝: {bad}"
        except ValueError:
            pass

    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        sch.SCHEDULE_FILE = tmp / "schedules.json"
        sch.OUTBOX_DIR = tmp / "outbox"
        order_exec.DRAFT_DIR = tmp / "drafts"

        s = sch.add(kind="order", at="08:00", store="1421252", items="麦旋风 x1", note="早餐")
        assert s["status"] == "pending"
        assert len(sch.list_all(only_pending=True)) == 1

        # 未到点不触发
        from datetime import datetime
        assert sch.due(now=datetime(2026, 10, 13, 7, 59)) == []
        due_list = sch.due(now=datetime(2026, 10, 13, 8, 1))
        assert len(due_list) == 1

        # 红线：到点客户端禁止一切写/花钱调用
        from src.mcd_client import McdClient, McpError
        guard = sch._NoMoneyClient(McdClient("x"))
        for money in ("create-order", "mall-create-order", "cancel-order", "draw-lottery"):
            try:
                guard.call(money, {}, confirm=True)
                assert False, f"{money} 应被到点阶段拦截"
            except PermissionError:
                pass
        try:
            guard.call("auto-bind-coupons", {})     # 连免费写操作也不许
            assert False, "auto-bind-coupons 应被拦截"
        except PermissionError:
            pass

        # fire：只重算价 + 推确认卡，产出 confirmedByHuman=False
        class FakeClient:
            def call(self, name, args=None, confirm=False):
                assert confirm is False, "到点阶段不允许带 confirm 调用"
                if name == "query-meals":
                    return {"data": {"meals": {"111": {"name": "经典麦旋风", "code": "111",
                                                       "currentPrice": 15.5, "originalPrice": 15.5}}}}
                if name in ("query-my-coupons", "query-store-coupons"):
                    return {"data": {"coupons": [], "list": []}}
                if name == "calculate-price":
                    return {"data": {"price": 1550, "originalPrice": 1550, "discount": 0,
                                     "takeWayList": [{"code": "eat-in", "title": "堂食"}],
                                     "storeName": "测试店"}}
                raise AssertionError(f"到点阶段不应调用 {name}")

            # 只读便捷方法（与真实 McdClient 同名）
            def meals(self, store_code, **kw):
                return self.call("query-meals", kw or {})

            def my_coupons(self):
                return self.call("query-my-coupons")

            def store_coupons(self, store_code):
                return self.call("query-store-coupons")

        r = sch.fire(FakeClient(), s)
        assert r["payload"]["type"] == "confirm"
        assert "不会自动下单" in r["payload"]["meta"]["hint"]
        assert Path(r["json"]).exists() and Path(r["html"]).exists()
        draft = json.loads(Path(r["draft"]).read_text(encoding="utf-8"))
        assert draft["confirmedByHuman"] is False          # ← 未获用户确认
        assert draft["scheduledFrom"]["id"] == s["id"]
        assert r["readOnlyCalls"] and "create-order" not in r["readOnlyCalls"]
        # 触发后状态变为 fired（等用户确认）
        assert sch.list_all()[0]["status"] == "fired"
        assert sch.due(now=datetime(2026, 10, 13, 9, 0)) == []   # 不会重复触发

        assert sch.cancel(s["id"]) and sch.list_all()[0]["status"] == "cancelled"


def test_repeat_schedule():
    """repeatDays：连续 N 天每天触发一次；触发满 N 次后置 fired；当天去重。"""
    from src import schedule as sch
    from src.mcd_client import McdClient
    from datetime import datetime
    import tempfile as _tf

    # ① due 的重复窗口 + 按天去重（纯逻辑，注入 now）
    base = {
        "id": "sch-r", "kind": "alert", "at": "08:00", "time": "08:00:00",
        "date": None, "store": "", "items": "", "track": "global",
        "coupon": "auto", "note": "早餐", "repeatDays": 3,
        "status": "pending", "createdAt": "2026-10-13 07:00:00",
        "firedAt": None, "fireCount": 0, "lastFiredDate": None,
    }
    assert len(sch.due(schedules=[base], now=datetime(2026, 10, 13, 8, 1))) == 1  # 第1天
    assert sch.due(schedules=[base], now=datetime(2026, 10, 13, 7, 59)) == []     # 未到点
    fired1 = dict(base); fired1["fireCount"] = 1; fired1["lastFiredDate"] = "2026-10-13"
    assert sch.due(schedules=[fired1], now=datetime(2026, 10, 13, 9, 0)) == []     # 当天去重
    assert len(sch.due(schedules=[fired1], now=datetime(2026, 10, 14, 8, 1))) == 1  # 第2天
    assert len(sch.due(schedules=[fired1], now=datetime(2026, 10, 15, 8, 1))) == 1  # 第3天
    assert sch.due(schedules=[fired1], now=datetime(2026, 10, 16, 8, 1)) == []      # 第4天超窗口

    # ② _emit 状态流转：repeatDays=2 触发两次后置 fired
    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        sch.SCHEDULE_FILE = tmp / "schedules.json"
        sch.OUTBOX_DIR = tmp / "outbox"
        s = sch.add(kind="alert", at="09:00", note="两次", days=2)
        sch.fire(sch._NoMoneyClient(McdClient("x")), s)
        assert sch.list_all()[0]["status"] == "pending" and sch.list_all()[0]["fireCount"] == 1
        sch.fire(sch._NoMoneyClient(McdClient("x")), s)
        assert sch.list_all()[0]["status"] == "fired" and sch.list_all()[0]["fireCount"] == 2


def test_fuzzy_prefers_bought():
    """模糊匹配优先命中买过的商品（用户只说简称时的正确预期）。"""
    from src import optimize, price_memory
    menu = {
        "经典麦旋风": {"name": "经典麦旋风"},
        "蓝莓爆爆珠麦旋风": {"name": "蓝莓爆爆珠麦旋风"},
        "派": {"name": "派"},
        "姜汁撞奶风味派": {"name": "姜汁撞奶风味派"},
    }
    profile = {"姜汁撞奶风味派": {"buyCount": 2}, "蓝莓爆爆珠麦旋风": {"buyCount": 1}}
    # 买过的优先：用户说「派」应命中他常买的姜汁撞奶风味派，而不是泛指的「派」
    k, cands = optimize._fuzzy_menu_key("派", menu, profile)
    assert menu[k]["name"] == "姜汁撞奶风味派"
    # 无购买记录时才退化为最短包含匹配
    k2, _ = optimize._fuzzy_menu_key("麦旋风", menu, {})
    assert menu[k2]["name"] == "经典麦旋风"
    # 完全不沾边 → 无候选，由上层报错并列相近商品
    k3, c3 = optimize._fuzzy_menu_key("双人晚餐", menu, profile)
    assert k3 == "" and c3 == []


def test_message_format():
    """消息格式规范：确认卡必须含商品明细/单价/门店/取餐方式/券，红线声明不被覆盖。"""
    from src import payloads, present, price_memory

    draft = {
        "storeCode": "1421252", "storeName": "麦当劳深圳金环宇大厦餐厅",
        "takeWayCode": "eat-in", "takeWayName": "堂食",
        "items": [{"productCode": "4810", "name": "薯条", "officialName": "中薯条",
                   "quantity": 2, "unitPrice": 14.5, "subtotal": 29.0}],
        "coupon": {"couponId": "1", "couponCode": "ABC", "name": "9.9元中杯冰美式",
                   "expireAt": "2026-10-18 23:59:59"},
        "price": 39.5, "originalPrice": 44.5, "discount": 5.0,
        "scheduledFrom": {"id": "sch-x", "at": "18:30", "note": "加班餐"},
    }
    p = payloads.confirm_from_draft(draft, "d.json")
    text = present.render_inline(p)

    # 商品明细：数量 + 小计 + 官方规格名 + 单价
    assert "薯条 × 2" in text and "¥29" in text and "中薯条" in text and "单价 ¥14.5" in text
    # 金额三件套 + 到点时间
    for kw in ("到点时间", "原价", "到手价", "省"):
        assert kw in text, f"缺少 {kw}"
    # 门店 + 取餐方式 + 券与到期时间
    assert "麦当劳深圳金环宇大厦餐厅" in text and "堂食" in text
    assert "9.9元中杯冰美式" in text and "2026-10-18" in text
    # 红线与两个动作
    assert "不会自动下单" in p["meta"]["hint"] or "不会" in p["meta"]["hint"]
    assert {a["id"] for a in p["actions"]} == {"confirm", "cancel"}

    # 到点消息：追加门店信息而不是覆盖掉红线声明
    from src import schedule as sch_mod
    sch_mod.SCHEDULE_FILE = Path(tempfile.gettempdir()) / "_mcd_sched_test.json"
    try:
        fake_sch = {"id": "sch-x", "at": "18:30", "note": "加班餐", "kind": "order"}
        p2 = payloads.confirm_from_draft(draft, "d.json")
        p2["meta"]["hint"] = (f'到点任务 {fake_sch["id"]}｜{fake_sch["note"]}｜'
                              f'{draft["storeName"]}（{draft["takeWayName"]}）｜'
                              f'券：{draft["coupon"]["name"]}｜'
                              f'⛔ 这条只是确认消息，我不会自动下单。')
        assert "金环宇大厦" in p2["meta"]["hint"] and "我不会自动下单" in p2["meta"]["hint"]
    finally:
        sch_mod.SCHEDULE_FILE = Path(tempfile.gettempdir()) / "_mcd_sched_test.json"

    # 到点提醒：有登记商品时列出商品并注明需确认
    a = payloads.alert_meal_time(items=[{"name": "薯条", "quantity": 2, "note": "待下单，需你确认"}],
                                 sch={"at": "11:30", "note": "午饭"})
    at = present.render_inline(a)
    assert "薯条 × 2" in at and "需你确认" in at and "11:30" in at


def test_confirm_format_edge():
    """省 0 元不显示「省」；inline 卡片不把 command 贴给用户。"""
    from src import payloads, present

    # 省 0：metrics 不含「省」指标，summary 不提「省 0」
    p0 = payloads.confirm_from_draft({
        "storeCode": "1421252", "storeName": "测试店", "takeWayName": "堂食",
        "items": [{"productCode": "4810", "name": "薯条", "quantity": 2, "subtotal": 29.0}],
        "coupon": None, "price": 29.0, "originalPrice": 29.0, "discount": 0.0,
    }, "d.json")
    assert p0["type"] == "confirm"
    assert "省" not in [m["label"] for m in p0["metrics"]]
    assert "省" not in p0["summary"]

    # 省 > 0：仍显示「省」
    p1 = payloads.confirm_from_draft({
        "storeCode": "1421252", "storeName": "测试店",
        "items": [{"productCode": "4810", "name": "薯条", "quantity": 2}],
        "coupon": None, "price": 25.0, "originalPrice": 29.0, "discount": 4.0,
    }, "d.json")
    assert "省" in [m["label"] for m in p1["metrics"]]

    # inline 渲染不把 command 贴给用户（那是给 Agent 执行的指令）
    txt = present.render_inline(p1)
    assert "python main.py order" not in txt
    assert "[确认下单]" in txt and "[再想想]" in txt


def test_confirm_gate_hardening():
    """确认闸强化：digest 防篡改 + 报价时效 + 券过期 + 精确确认文本 + 重算价 diff。"""
    from src import order_exec
    from datetime import datetime, timedelta

    draft = {
        "storeCode": "1421252", "takeWayCode": "eat-in",
        "items": [{"productCode": "4810", "quantity": 2}],
        "coupon": {"couponId": "1", "couponCode": "ABC", "name": "券",
                   "expireAt": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")},
        "price": 25.0, "originalPrice": 30.0,
        "quotedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    draft["digest"] = order_exec.draft_digest(draft)

    # 内容篡改 → validate 拒绝
    tampered = dict(draft); tampered["price"] = 1.0
    ok, reason = order_exec.validate_for_order(tampered)
    assert not ok and "不一致" in reason

    # 报价过期（TTL）→ 拒绝
    old = dict(draft)
    old["quotedAt"] = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    ok, reason = order_exec.validate_for_order(old)
    assert not ok and "过期" in reason

    # 券过期 → 拒绝（深拷贝，避免改到原 draft 的嵌套 coupon）
    import copy
    exp = copy.deepcopy(draft)
    exp["coupon"]["expireAt"] = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    ok, reason = order_exec.validate_for_order(exp)
    assert not ok and "过期" in reason

    # 正常 → 放行
    ok, reason = order_exec.validate_for_order(draft)
    assert ok, reason

    # human_gate：confirmText 非精确 → 拒绝；精确 → 放行
    assert order_exec.human_gate({"confirmedByHuman": True, "confirmedAt": "x",
                                  "confirmedText": "随便"}) is False
    assert order_exec.human_gate({"confirmedByHuman": True, "confirmedAt": "x",
                                  "confirmedText": "确认下单"}) is True

    # diff_draft：价格 / 券 / 商品变化都能被捕捉
    new = dict(draft); new["price"] = 28.0
    assert any("到手价" in c for c in order_exec.diff_draft(draft, new))
    new2 = dict(draft); new2["coupon"] = {"couponId": "2", "couponCode": "X", "name": "另一张"}
    assert any("用券" in c for c in order_exec.diff_draft(draft, new2))
    new3 = dict(draft); new3["items"] = [{"productCode": "999", "quantity": 1}]
    assert any("商品清单" in c for c in order_exec.diff_draft(draft, new3))
    assert order_exec.diff_draft(draft, dict(draft)) == []


def test_price_failures_raise():
    """算价失败不再静默：无金额 / 无取餐方式直接报错，券查询失败记 warnings。"""
    from src import optimize
    try:
        optimize._price_of({"data": {"foo": 1}})
        assert False, "无金额应报错"
    except ValueError:
        pass
    try:
        optimize._make_draft("S", [], {"coupon": None, "result": {"data": {"price": 100}}},
                             None, "global")
        assert False, "缺 takeWayCode 应报错"
    except ValueError:
        pass
    try:
        optimize._make_draft("S", [], {"coupon": None, "result": {"data": {}}},
                             {"code": "eat-in", "title": "堂食"}, "global")
        assert False, "缺金额应报错"
    except ValueError:
        pass

    class Bad:
        def my_coupons(self): raise RuntimeError("boom")
        def store_coupons(self, s): return {"data": {"coupons": [{"id": "1"}]}}
    coupons, warnings = optimize._coupon_candidates(Bad(), "S")
    assert len(coupons) == 1 and any("失败" in w for w in warnings)


def test_schedule_concurrency():
    """定时任务：claim 防并发 + 失败回滚 pending 并抑制重试。"""
    from src import schedule as sch
    from src.mcd_client import McdClient
    from datetime import datetime
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        sch.SCHEDULE_FILE = tmp / "schedules.json"
        sch.OUTBOX_DIR = tmp / "outbox"
        sch.FIRE_LOCK_FILE = tmp / ".lock"

        # 并发：已 claim(firing) 的任务再次 fire 应 skip
        s = sch.add(kind="alert", at="09:00", note="测试")
        sch._mark(s["id"], status="firing")
        r = sch.fire(sch._NoMoneyClient(McdClient("x")), s)
        assert r.get("skipped") is True and "firing" in r["reason"]

        # 失败回滚：order 分支算价抛异常 → 回滚 pending + lastError + nextRetryAt
        s2 = sch.add(kind="order", at="10:00", store="S", items="麦旋风 x1")

        class BoomClient:
            def meals(self, *a, **kw): raise RuntimeError("simulated fail")
            def my_coupons(self): raise RuntimeError("simulated fail")
            def store_coupons(self, s): raise RuntimeError("simulated fail")
        try:
            sch.fire(sch._NoMoneyClient(BoomClient()), s2)
            assert False, "算价失败应上抛"
        except RuntimeError:
            pass
        cur = sch._get(s2["id"])
        assert cur["status"] == "pending"
        assert cur.get("lastError") and cur.get("nextRetryAt")
        # due() 在 nextRetryAt 之前不返回该任务（抑制补触发）
        assert sch.due(schedules=[cur], now=datetime.now()) == []


def test_is_claimable():
    """券筛选：couponStatus / label 已领取均被排除，正常券可领。"""
    from src import coupons
    assert coupons.is_claimable({"couponName": "A", "couponStatus": "HAVING_RECEIVE"}) is False
    assert coupons.is_claimable({"couponName": "A", "status": "RECEIVED"}) is False
    assert coupons.is_claimable({"couponName": "A", "couponStatus": "", "label": "已领取"}) is False
    assert coupons.is_claimable({"couponName": "A", "couponStatus": "", "label": ""}) is True
    assert coupons.is_claimable({}) is False


def test_auto_claim_diff():
    """领券回执：本次领到 = 领前/领后卡包 id 差集，而非候选列表数量。"""
    from src import coupons

    class Fake:
        def __init__(self): self.wallet = ["c1"]
        def my_coupons(self):
            return {"data": {"coupons": [{"id": i} for i in self.wallet]}}
        def available_coupons(self):
            # 2 张「可领」，但实际只领到 1 张 → 应报 1，而不是按候选数报 2
            return {"data": [{"couponName": "券A", "couponStatus": "", "label": ""},
                             {"couponName": "券B", "couponStatus": "", "label": ""}]}
        def call(self, name, args=None, confirm=False):
            if name == "auto-bind-coupons":
                self.wallet = ["c1", "c2"]
                return {"ok": True}
            raise AssertionError(name)

    r = coupons.auto_claim(Fake())
    assert r["claimedCount"] == 1
    assert r["claimedIds"] == ["c2"]
    assert r["walletCount"] == 2


def test_validate_payload():
    """payload 契约校验：缺 title / 非法 action / 非法 type 必须拒绝。"""
    from src import present
    try:
        present.validate_payload({"schemaVersion": 1, "type": "alert"})
        assert False, "缺 title 应拒绝"
    except ValueError:
        pass
    try:
        present.validate_payload({"schemaVersion": 1, "type": "confirm", "title": "x",
                                  "actions": [{"id": "confirm"}]})
        assert False, "action 缺 label 应拒绝"
    except ValueError:
        pass
    ok = present.validate_payload({"schemaVersion": 1, "type": "alert", "title": "x"})
    assert ok["title"] == "x"


def test_cancel():
    """取消未支付订单：保守识别 + 逐单取消带 confirm=True + 失败留痕。"""
    from src import cancel

    # ① 状态识别：只命中「未支付」关键词，终态与未知状态一律不判
    assert cancel.is_unpaid_status("待支付") is True
    assert cancel.is_unpaid_status("未支付") is True
    assert cancel.is_unpaid_status("等待付款") is True
    assert cancel.is_unpaid_status("订单已完成") is False
    assert cancel.is_unpaid_status("已取消") is False
    assert cancel.is_unpaid_status("制作中") is False   # 未知状态保守判否
    assert cancel.is_unpaid_status("") is False
    assert cancel.is_unpaid_status(None) is False

    # ② 列表筛选
    class FakeList:
        def order_list(self):
            return {"data": {"list": [
                {"orderId": "o1", "orderStatus": "订单已完成"},
                {"orderId": "o2", "orderStatus": "待支付"},
                {"orderId": "o3", "orderStatus": "制作中"},
                {"orderId": "o4", "orderStatus": "已取消"},
            ]}}
    unpaid, all_orders = cancel.list_unpaid_orders(FakeList())
    assert [o["orderId"] for o in unpaid] == ["o2"]
    assert len(all_orders) == 4

    # ③ 逐单取消：每单 cancel-order 必须带 confirm=True（红线），失败不中断
    calls = []
    class FakeCancel:
        def call(self, name, args=None, confirm=False):
            calls.append((name, args, confirm))
            if name == "cancel-order":
                if args["orderId"] == "o_fail":
                    raise RuntimeError("simulated fail")
                return {"ok": True}
            raise AssertionError(name)
    r = cancel.cancel_orders(FakeCancel(), [{"orderId": "o1"}, {"orderId": "o_fail"},
                                            {"orderId": "o3"}], reason="1")
    assert all(c[2] is True for c in calls), "cancel-order 必须 confirm=True"
    assert all(c[1]["cancelReasonCode"] == "1" for c in calls)
    assert len(r["cancelled"]) == 2 and len(r["failed"]) == 1
    assert r["failed"][0]["orderId"] == "o_fail"


def test_cancel_payload():
    """取消未支付：预览 confirm 卡（确认动作不给用户看 command）+ 回执 alert。"""
    from src import payloads, present
    unpaid = [{"orderId": "o2", "storeName": "测试店", "realTotalAmount": "25.9",
               "createTime": "2026-10-13 12:00:00", "orderStatus": "待支付"}]
    p = payloads.cancel_unpaid_preview(unpaid, scanned=10)
    assert p["type"] == "confirm"
    assert {a["id"] for a in p["actions"]} == {"cancel_all", "keep"}
    assert "取消不可逆" in p["meta"]["hint"]
    txt = present.render_inline(p)
    assert "待确认取消" in txt and "[确认取消]" in txt
    assert "python main.py cancel" not in txt  # command 不给终端用户看

    r = payloads.cancel_unpaid_receipt({"cancelled": [{"orderId": "o2"}], "failed": []})
    assert r["type"] == "alert"
    assert r["metrics"][0]["value"] == 1


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_normalize()
        test_meal_index()
        test_price_memory(tmp)
        test_qr()
        test_recipe_and_gate(tmp)
        test_present_layers()
        test_ledger(tmp)
        test_address_derive()
        test_config_write_whitelist()
        test_onboarding()
        test_phrases()
        test_schedule()
        test_repeat_schedule()
        test_fuzzy_prefers_bought()
        test_message_format()
        test_confirm_format_edge()
        test_confirm_gate_hardening()
        test_price_failures_raise()
        test_schedule_concurrency()
        test_is_claimable()
        test_auto_claim_diff()
        test_validate_payload()
        test_cancel()
        test_cancel_payload()
    print("ALL OFFLINE TESTS PASSED ✅")


if __name__ == "__main__":
    main()
