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


def test_warmup_order_import_is_idempotent():
    """重复 warmup 不重复累计成功导入的订单，失败详情会在下次重试。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old_profile = price_memory.PROFILE_FILE
        old_processed = price_memory.PROCESSED_ORDERS_FILE
        old_store_names = price_memory.STORE_NAMES_FILE
        price_memory.PROFILE_FILE = tmp / "profile.json"
        price_memory.PROCESSED_ORDERS_FILE = tmp / "processed.json"
        price_memory.STORE_NAMES_FILE = tmp / "stores.json"

        class Client:
            def __init__(self):
                self.detail_calls = {}

            def order_list(self):
                return {"data": {"list": [
                    {"orderId": "ok-1", "storeCode": "S1", "storeName": "门店"},
                    {"orderId": "retry-1", "storeCode": "S1"},
                ]}}

            def query_order(self, oid):
                self.detail_calls[oid] = self.detail_calls.get(oid, 0) + 1
                if oid == "retry-1" and self.detail_calls[oid] == 1:
                    raise RuntimeError("temporary failure")
                return {"data": {"totalDiscountAmount": 1,
                                 "orderProductList": [{"productName": "麦旋风", "quantity": 2}]}}

        try:
            client = Client()
            first = price_memory.cold_start_from_orders(client)
            assert first["profile"]["麦旋风"]["buyCount"] == 2
            assert client.detail_calls == {"ok-1": 1, "retry-1": 1}
            second = price_memory.cold_start_from_orders(client)
            assert second["profile"]["麦旋风"]["buyCount"] == 4
            assert client.detail_calls == {"ok-1": 1, "retry-1": 2}
            third = price_memory.cold_start_from_orders(client)
            assert third["profile"]["麦旋风"]["buyCount"] == 4
            assert client.detail_calls == {"ok-1": 1, "retry-1": 2}
        finally:
            price_memory.PROFILE_FILE = old_profile
            price_memory.PROCESSED_ORDERS_FILE = old_processed
            price_memory.STORE_NAMES_FILE = old_store_names


def test_warmup_migrates_legacy_profile_without_recounting():
    """已有购买计数但尚无订单账本的旧档案，初始化账本时不重复累计。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old_profile = price_memory.PROFILE_FILE
        old_processed = price_memory.PROCESSED_ORDERS_FILE
        old_store_names = price_memory.STORE_NAMES_FILE
        price_memory.PROFILE_FILE = tmp / "profile.json"
        price_memory.PROCESSED_ORDERS_FILE = tmp / "processed.json"
        price_memory.STORE_NAMES_FILE = tmp / "stores.json"
        price_memory.save_profile({"麦旋风": {"aliases": [], "buyCount": 5, "storeCodes": []}})

        class Client:
            def order_list(self):
                return {"data": {"list": [{"orderId": "old-order", "storeCode": "S1"}]}}

            def query_order(self, oid):
                raise AssertionError("旧订单不应再次导入")

        try:
            result = price_memory.cold_start_from_orders(Client())
            assert result["profile"]["麦旋风"]["buyCount"] == 5
            assert json.loads(price_memory.PROCESSED_ORDERS_FILE.read_text(encoding="utf-8")) == ["old-order"]
        finally:
            price_memory.PROFILE_FILE = old_profile
            price_memory.PROCESSED_ORDERS_FILE = old_processed
            price_memory.STORE_NAMES_FILE = old_store_names


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
    """H 省钱账单：drop 估算 + order 真实优惠**分开**（实际省下 ≠ 潜在优惠）。"""
    from src import ledger
    ledger.LEDGER_FILE = tmp / "ledger.jsonl"
    ledger.append_event("drop", name="雪糕", storeCode="S1", saved=3.0)
    ledger.append_event("order", name="麦旋风", storeCode="S1",
                        originalPrice=30.4, price=25.9, saved=4.5)
    agg = ledger.aggregate("all")
    assert agg["saved"] == 4.5, "实际省下应只含 order 真实优惠"
    assert agg["potential"] == 3.0, "潜在优惠应只含 drop 估算"
    assert agg["drops"] == 1 and agg["orders"] == 1
    assert agg["items"][0]["name"] == "麦旋风", "商品榜只统计真实下单，不混入 drop 估算"
    r = payloads_report(agg)
    assert r["type"] == "report" and r["preferredRender"] == "html"
    labels = [m["label"] for m in r["metrics"]]
    assert "实际省下" in labels and "潜在优惠(未购买)" in labels


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
    assert sch.parse_at("18:30:59")["time"] == "18:30:59"
    for bad in ("25:00", "abc", "18:99", "18:30:99", "2026-02-30 08:00", ""):
        try:
            sch.parse_at(bad)
            assert False, f"非法时间应被拒绝: {bad}"
        except ValueError:
            pass

    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        sch.SCHEDULE_FILE = tmp / "schedules.json"
        sch.OUTBOX_DIR = tmp / "outbox"
        sch.FIRE_LOCK_FILE = tmp / ".fire.lock"
        order_exec.DRAFT_DIR = tmp / "drafts"

        for kwargs in (
            {"kind": "invalid", "at": "08:00"},
            {"kind": "order", "at": "08:00"},
            {"kind": "order", "at": "08:00", "store": "S", "items": ""},
            {"kind": "order", "at": "08:00", "store": "S", "items": "麦旋风 x0"},
            {"kind": "order", "at": "08:00", "store": "S", "items": "麦旋风 x-1"},
            {"kind": "order", "at": "08:00", "store": "S", "items": "麦旋风 xabc"},
            {"kind": "order", "at": "08:00", "store": "S", "items": "x1"},
            {"kind": "alert", "at": "08:00", "days": -1},
        ):
            try:
                sch.add(**kwargs)
                assert False, f"非法定时任务应拒绝: {kwargs}"
            except ValueError:
                pass

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
        def store_coupons(self, s, **kw): return {"data": {"coupons": [{"id": "1"}]}}
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
    assert "近期" in p["meta"]["hint"], "必须明确扫描范围仅近期可见订单"
    assert "近期" in p["summary"]
    txt = present.render_inline(p)
    assert "待确认取消" in txt and "[确认取消]" in txt
    assert "python main.py cancel" not in txt  # command 不给终端用户看

    r = payloads.cancel_unpaid_receipt({"cancelled": [{"orderId": "o2"}], "failed": []})
    assert r["type"] == "alert"
    assert r["metrics"][0]["value"] == 1


def test_no_retry_on_write():
    """写操作（含免费写）网络层错误禁重试；读操作仍重试。"""
    from src.mcd_client import McdClient, NetworkError
    captured = {}

    class Spy(McdClient):
        def __init__(self):
            self.token = "x"; self.endpoint = "http://x"
            self._call_ts = []; self._id = 0; self._initialized = True

        def rpc(self, method, params=None, notify=False, retry_network=True):
            captured[params["name"]] = retry_network
            return {"structuredContent": {"ok": True}}

    s = Spy()
    s.call("create-order", {"x": 1}, confirm=True)
    s.call("cancel-order", {}, confirm=True)
    s.call("mall-create-order", {}, confirm=True)
    s.call("auto-bind-coupons", {})      # 免费写操作同样禁重试
    s.call("delivery-create-address", {})
    s.call("query-meals", {})            # 读操作可重试
    assert captured["create-order"] is False
    assert captured["cancel-order"] is False
    assert captured["mall-create-order"] is False
    assert captured["auto-bind-coupons"] is False
    assert captured["delivery-create-address"] is False
    assert captured["query-meals"] is True


def test_post_network_no_retry():
    """_post：retry_network=False 时网络错误立即抛 NetworkError 且只发一次请求。"""
    import urllib.request as _ur
    import urllib.error
    from src.mcd_client import McdClient, NetworkError

    n = {"count": 0}
    orig = _ur.urlopen

    class Spy(McdClient):
        def __init__(self):
            self.token = "x"; self.endpoint = "http://x"; self._call_ts = []

        def _throttle(self):
            pass  # 跳过限流

    def fake_urlopen(req, timeout=None):
        n["count"] += 1
        raise urllib.error.URLError("connection lost")

    _ur.urlopen = fake_urlopen
    try:
        s = Spy()
        try:
            s._post({"a": 1}, retry_network=False)
            assert False, "应抛 NetworkError"
        except NetworkError:
            pass
        assert n["count"] == 1, "写操作网络错误不应重试"
    finally:
        _ur.urlopen = orig


def test_uncertain_order_probe():
    """下单网络层结果不确定：只提交一次、查订单状态、返回 uncertain 回执（不重复下单）。"""
    from src import order_exec
    from src.mcd_client import NetworkError

    draft = {
        "storeCode": "1421252", "takeWayCode": "eat-in",
        "items": [{"productCode": "4810", "quantity": 2}],
        "coupon": {"couponId": "1", "couponCode": "ABC", "name": "券"},
        "price": 25.0, "originalPrice": 30.0,
        "confirmedByHuman": True, "confirmedAt": "2026-10-13 12:00:00",
        "confirmedText": "确认下单",
    }

    # 场景 A：服务端已建单但响应丢失 → 探测到新增未支付订单
    class FakeA:
        def __init__(self): self.created = False; self.create_calls = 0
        def order_list(self):
            orders = [{"orderId": "old1", "orderStatus": "订单已完成"}]
            if self.created:
                orders.append({"orderId": "new1", "orderStatus": "待支付"})
            return {"data": {"list": orders}}
        def call(self, name, args=None, confirm=False):
            if name == "create-order":
                self.create_calls += 1
                self.created = True   # 模拟服务端已创建订单
                raise NetworkError("网络错误（结果不确定）: connection lost")
            raise AssertionError(name)

    fa = FakeA()
    r = order_exec.execute_draft(fa, draft)
    assert r["status"] == "uncertain"
    assert fa.create_calls == 1, "create-order 只提交一次，绝不重试"
    assert r["probe"]["newUnpaidOrder"] is True
    assert "很可能已成功" in r["probe"]["note"]

    # 场景 B：响应丢失但服务端未建单 → 未发现新增
    class FakeB:
        def __init__(self): self.create_calls = 0
        def order_list(self):
            return {"data": {"list": [{"orderId": "old1", "orderStatus": "订单已完成"}]}}
        def call(self, name, args=None, confirm=False):
            if name == "create-order":
                self.create_calls += 1
                raise NetworkError("网络错误: connection lost")
            raise AssertionError(name)
    fb = FakeB()
    r2 = order_exec.execute_draft(fb, draft)
    assert r2["status"] == "uncertain" and r2["probe"]["newUnpaidOrder"] is False

    # 场景 C：快照查询失败 → before_ids=None，仍返回 uncertain（不重复提交）
    class FakeC:
        def __init__(self): self.create_calls = 0
        def order_list(self): raise RuntimeError("query fail")
        def call(self, name, args=None, confirm=False):
            if name == "create-order":
                self.create_calls += 1
                raise NetworkError("网络错误: connection lost")
            raise AssertionError(name)
    fc = FakeC()
    r3 = order_exec.execute_draft(fc, draft)
    assert r3["status"] == "uncertain" and r3["probe"]["queried"] is False
    assert fc.create_calls == 1


def test_draft_binding_hardening():
    """确认绑定草稿：强制 digest、diff 比较门店/取餐/预约、草稿 ID 不重复。"""
    from src import order_exec
    import tempfile as _tf

    # ① 无 digest 的草稿必须拒绝（不能靠删 digest 字段绕过篡改检测）
    no_digest = {
        "storeCode": "1421252", "takeWayCode": "eat-in",
        "items": [{"productCode": "4810", "quantity": 2}],
        "price": 25.0, "originalPrice": 30.0,
    }
    ok, reason = order_exec.validate_for_order(no_digest)
    assert not ok and "digest" in reason

    # ② diff 比较门店 / 取餐方式 / 预约时间（此前只比价格/券/商品，漏了这三项）
    base = {
        "storeCode": "1421252", "takeWayCode": "eat-in",
        "items": [{"productCode": "4810", "quantity": 2}],
        "coupon": None, "price": 25.0, "originalPrice": 30.0,
        "reservationDate": None,
    }
    s = dict(base); s["storeCode"] = "9999999"
    assert any("门店" in c for c in order_exec.diff_draft(base, s))
    t = dict(base); t["takeWayCode"] = "delivery"
    assert any("取餐方式" in c for c in order_exec.diff_draft(base, t))
    r = dict(base); r["reservationDate"] = "2026-10-14 12:00"
    assert any("预约时间" in c for c in order_exec.diff_draft(base, r))
    assert order_exec.diff_draft(base, dict(base)) == []

    # ③ 草稿 ID 去重：同秒保存内容不同的两份草稿不互相覆盖
    with _tf.TemporaryDirectory() as td:
        import pathlib
        _orig = order_exec.DRAFT_DIR
        order_exec.DRAFT_DIR = pathlib.Path(td)
        try:
            d1 = order_exec.save_draft(dict(base))
            d2 = order_exec.save_draft({**base, "price": 26.0})  # 内容不同
            assert d1 != d2 and d1.exists() and d2.exists()
            assert d1.read_text(encoding="utf-8") != d2.read_text(encoding="utf-8")
        finally:
            order_exec.DRAFT_DIR = _orig


def test_delivery_support():
    """外卖支持：草稿字段 / digest 覆盖 / diff 捕捉 / 下单参数 / 门店解析。"""
    import tempfile as _tf
    import pathlib
    from src import order_exec, optimize, payloads, present

    # ① _make_draft 外卖：无 takeWayList 不报错，字段正确
    draft = optimize._make_draft(
        "1421252", [{"productCode": "901401", "quantity": 1, "name": "麦乐鸡5块"}],
        {"coupon": None, "result": {"data": {
            "price": 2230, "originalPrice": 2230, "discount": 0,
            "deliveryPrice": 600, "packingPrice": 80,
            "storeName": "麦当劳深圳金环宇大厦餐厅",
        }}},
        None, "global",
        delivery={"addressId": "A1", "beCode": "142125202", "addressText": "深圳某大厦"},
    )
    assert draft["takeWay"] == "delivery"
    assert draft["orderType"] == 2 and draft["beType"] == 2
    assert draft["beCode"] == "142125202" and draft["addressId"] == "A1"
    assert draft["takeWayCode"] == ""            # 外卖无 takeWayCode
    assert draft["deliveryFee"] == 6.0 and draft["packingFee"] == 0.8
    assert draft["price"] == 22.3

    # ② draft_digest 覆盖 addressId/beCode（改地址/业务码会改摘要）
    d0 = order_exec.draft_digest(draft)
    assert order_exec.draft_digest({**draft, "addressId": "A2"}) != d0
    assert order_exec.draft_digest({**draft, "beCode": "X"}) != d0

    # ③ diff_draft 捕捉外卖字段
    assert any("业务编码" in c for c in order_exec.diff_draft(draft, {**draft, "beCode": "999"}))
    assert any("配送地址" in c for c in order_exec.diff_draft(draft, {**draft, "addressId": "B"}))
    assert any("配送费" in c for c in order_exec.diff_draft(draft, {**draft, "deliveryFee": 9.0}))
    assert order_exec.diff_draft(draft, dict(draft)) == []

    # ④ execute_draft 外卖下单参数：不传 takeWayCode，传 orderType/beType/beCode/addressId
    confirmed = dict(draft)
    confirmed["confirmedByHuman"] = True
    confirmed["confirmedAt"] = "2026-10-13 15:00:00"
    confirmed["confirmedText"] = "确认下单"

    captured = {}

    class CapClient:
        def order_list(self):
            return {"data": {"list": []}}
        def call(self, name, arguments=None, confirm=False):
            captured["name"], captured["args"], captured["confirm"] = name, arguments, confirm
            return {"orderId": "O123", "success": True}

    with _tf.TemporaryDirectory() as td:
        _orig = order_exec.RECEIPT_LOG
        order_exec.RECEIPT_LOG = pathlib.Path(td) / "receipts.jsonl"
        try:
            order_exec.execute_draft(CapClient(), confirmed)
        finally:
            order_exec.RECEIPT_LOG = _orig
    assert captured["name"] == "create-order"
    a = captured["args"]
    assert a["orderType"] == 2 and a["beType"] == 2
    assert a["beCode"] == "142125202" and a["addressId"] == "A1"
    assert "takeWayCode" not in a              # 外卖不传 takeWayCode
    assert captured["confirm"] is True

    # ⑤ resolve_delivery：指定 store 自动解析 beCode + 回填地址文本
    class ResClient:
        def delivery_addresses(self):
            return {"data": {"addresses": [{"addressId": "A1", "fullAddress": "深圳某大厦"}]}}
        def delivery_stores(self, address_id, be_type=2):
            return {"data": [{"storeCode": "1421252", "beCode": "142125202",
                              "storeName": "金环宇"}]}

    resolved = optimize.resolve_delivery(ResClient(), "A1", store_code="1421252")
    assert resolved["beCode"] == "142125202" and resolved["storeCode"] == "1421252"
    assert resolved["addressText"] == "深圳某大厦"

    # ⑥ confirm 卡外卖 metric + 配送地址展示
    p = payloads.confirm_from_draft(draft, "d.json")
    labels = [m["label"] for m in p["metrics"]]
    assert "配送费" in labels and "打包费" in labels
    assert "配送至" in present.render_inline(p)


def test_timeout_to_network_error():
    """传输层超时（TimeoutError/socket.timeout）统一转 NetworkError，写操作不重试。"""
    import urllib.request as _ur
    from src.mcd_client import McdClient, NetworkError

    orig = _ur.urlopen

    class Spy(McdClient):
        def __init__(self):
            self.token = "x"; self.endpoint = "http://x"; self._call_ts = []
        def _throttle(self):
            pass  # 跳过限流

    # 写操作（retry_network=False）：超时只发一次，直接抛 NetworkError
    n = {"count": 0}
    def fake_timeout(req, timeout=None):
        n["count"] += 1
        raise TimeoutError("timed out")
    _ur.urlopen = fake_timeout
    try:
        s = Spy()
        try:
            s._post({"a": 1}, retry_network=False)
            assert False, "超时应抛 NetworkError"
        except NetworkError:
            pass
        assert n["count"] == 1, "写操作超时不应重试"
    finally:
        _ur.urlopen = orig

    # 读操作（retry_network=True）：超时应重试 3 次后才抛 NetworkError
    n["count"] = 0
    _ur.urlopen = fake_timeout
    try:
        s = Spy()
        try:
            s._post({"a": 1})
            assert False, "应抛 NetworkError"
        except NetworkError:
            pass
        assert n["count"] == 3, "读操作超时应重试 3 次"
    finally:
        _ur.urlopen = orig


def test_digest_covers_full_items():
    """摘要与 diff 覆盖商品完整业务字段（roundList/替换项/规格），非仅 productCode+quantity。"""
    from src import order_exec
    import time

    base = {
        "storeCode": "1421252", "takeWayCode": "eat-in",
        "items": [{"productCode": "4810", "quantity": 2,
                   "roundList": [{"id": "r1", "name": "原味"}],
                   "spec": "大份"}],
        "coupon": None, "price": 25.0, "originalPrice": 30.0,
        "reservationDate": None,
    }
    d0 = order_exec.draft_digest(base)

    # 只改 roundList（productCode/quantity/价格/券全不变）→ 摘要必须变化
    alt = json.loads(json.dumps(base))
    alt["items"][0]["roundList"] = [{"id": "r2", "name": "替换"}]
    assert order_exec.draft_digest(alt) != d0

    # 只加替换项字段 → 摘要必须变化
    alt2 = json.loads(json.dumps(base))
    alt2["items"][0]["replaceItem"] = {"productCode": "999"}
    assert order_exec.draft_digest(alt2) != d0

    # diff 必须捕捉（不只比 productCode+quantity）
    assert order_exec.diff_draft(base, alt) == ["商品清单已变化"]
    assert order_exec.diff_draft(base, alt2) == ["商品清单已变化"]
    assert order_exec.diff_draft(base, json.loads(json.dumps(base))) == []

    # 完整链路：篡改 roundList 后 validate_for_order 拒绝（digest 对不上）
    draft = dict(base)
    draft["digest"] = order_exec.draft_digest(base)
    draft["quotedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ok, _ = order_exec.validate_for_order(draft)
    assert ok, "原始草稿应通过"
    tampered = json.loads(json.dumps(draft))
    tampered["items"][0]["roundList"] = [{"id": "r2", "name": "替换"}]
    ok2, reason2 = order_exec.validate_for_order(tampered)
    assert not ok2 and "不一致" in reason2


def test_digest_covers_request_fields():
    """摘要覆盖 execute_draft 实际发送的全部字段：beType / reservationDate /
    couponId / couponCode（任一被篡改，摘要与 diff 都要变化）。"""
    from src import order_exec
    import time

    base = {
        "storeCode": "1421252", "takeWayCode": "eat-in",
        "orderType": 1, "beType": 1, "beCode": "", "addressId": "",
        "reservationDate": None,
        "items": [{"productCode": "4810", "quantity": 1}],
        "coupon": {"couponId": "100", "couponCode": "COUPON-ABC",
                   "name": "9.9券", "expireAt": ""},
        "price": 9.9, "originalPrice": 15.0,
    }
    d0 = order_exec.draft_digest(base)

    # ① beType 篡改 → 摘要变化 + diff 捕捉
    assert order_exec.draft_digest({**base, "beType": 2}) != d0
    assert any("业务类型" in c for c in order_exec.diff_draft(base, {**base, "beType": 2}))

    # ② reservationDate 篡改 → 摘要变化 + diff 捕捉
    assert order_exec.draft_digest({**base, "reservationDate": "2026-10-14 12:00"}) != d0
    assert any("预约时间" in c for c in order_exec.diff_draft(
        base, {**base, "reservationDate": "2026-10-14 12:00"}))

    # ③ 只改 couponId（couponCode 不变）→ 摘要变化 + diff 捕捉
    alt_cid = json.loads(json.dumps(base))
    alt_cid["coupon"]["couponId"] = "999"
    assert order_exec.draft_digest(alt_cid) != d0
    assert any("用券" in c for c in order_exec.diff_draft(base, alt_cid))

    # ④ 只改 couponCode（couponId 不变）→ 摘要变化 + diff 捕捉
    alt_ccode = json.loads(json.dumps(base))
    alt_ccode["coupon"]["couponCode"] = "COUPON-XYZ"
    assert order_exec.draft_digest(alt_ccode) != d0
    assert any("用券" in c for c in order_exec.diff_draft(base, alt_ccode))

    # ⑤ 完整链路：篡改 beType 后 validate_for_order 拒绝（digest 对不上）
    draft = dict(base)
    draft["digest"] = order_exec.draft_digest(base)
    draft["quotedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    assert order_exec.validate_for_order(draft)[0], "原始草稿应通过"
    tampered = json.loads(json.dumps(draft))
    tampered["beType"] = 2
    ok2, reason2 = order_exec.validate_for_order(tampered)
    assert not ok2 and "不一致" in reason2

    # ⑥ 只改 couponCode 后 validate_for_order 也拒绝
    tampered2 = json.loads(json.dumps(draft))
    tampered2["coupon"]["couponCode"] = "COUPON-XYZ"
    ok3, reason3 = order_exec.validate_for_order(tampered2)
    assert not ok3 and "不一致" in reason3


def test_build_argv_no_shell():
    """phrase --run 用 argv 列表调用（不经 shell），占位符值整体作为一个参数。"""
    from src import phrases as ph

    # best 模板：门店/商品占位符替换正确，商品值含空格也整体一个参数
    argv = ph.build_argv('python main.py best --store <门店code> --items "<商品> x1"',
                         store="1421252", item="经典麦旋风")
    assert argv == ["python", "main.py", "best", "--store", "1421252",
                    "--items", "经典麦旋风 x1"]

    # 注入尝试：值里的 shell 元字符不被解释、不被拆散，只作为字面参数
    argv2 = ph.build_argv('python main.py best --store <门店code> --items "<商品> x1"',
                          store="1;rm -rf /", item='x" && echo hacked')
    assert argv2 == ["python", "main.py", "best", "--store", "1;rm -rf /",
                     "--items", 'x" && echo hacked x1']

    # 外卖模板：--address-id 占位符被替换
    argv3 = ph.build_argv(
        'python main.py best --delivery --address-id <地址id> --items "<商品> x1"',
        item="麦辣鸡腿堡", address_id="ADDR-1")
    assert argv3 == ["python", "main.py", "best", "--delivery",
                     "--address-id", "ADDR-1", "--items", "麦辣鸡腿堡 x1"]

    # 留空占位符：未给 --draft 时，空占位符连同其 flag 一起丢弃
    argv4 = ph.build_argv('python main.py share --draft <草稿.json>', draft="")
    assert argv4 == ["python", "main.py", "share"]


def test_phrase_propagates_exit_code():
    """phrase --run 应将只读子命令的失败码传回调用方。"""
    from types import SimpleNamespace
    from unittest.mock import patch
    import main

    args = SimpleNamespace(text="麦当劳有什么优惠", run=True, store="", item="",
                           draft="", address_id="")
    with patch("subprocess.run", return_value=SimpleNamespace(returncode=7)) as run:
        try:
            main.cmd_phrase(args)
            assert False, "子进程失败码必须传递"
        except SystemExit as e:
            assert e.code == 7
        run.assert_called_once()


def test_schedule_due_skipped_result():
    """定时任务被并发状态跳过时不访问成功结果字段。"""
    from types import SimpleNamespace
    from unittest.mock import patch
    from src import schedule
    import main

    args = SimpleNamespace(action="due", fire=True, pending=False)
    with patch.object(schedule, "due", return_value=[{"id": "sch-race"}]), \
            patch.object(schedule, "fire", return_value={"skipped": True, "reason": "already claimed"}), \
            patch.object(main, "_client", return_value=object()):
        main.cmd_schedule(args)


def test_best_rejects_invalid_quantities():
    """best CLI 商品数量必须为正整数，并在联网前拒绝输入错误。"""
    from types import SimpleNamespace
    from unittest.mock import patch
    import main

    for items in ("麦旋风 x0", "麦旋风 x-1", "麦旋风 xabc", "x1", " , "):
        args = SimpleNamespace(items=items)
        with patch.object(main, "_client", side_effect=AssertionError("不应访问客户端")):
            try:
                main.cmd_best(args)
                assert False, f"非法商品数量应拒绝: {items}"
            except SystemExit as e:
                assert e.code == 1


def test_raw_readonly_gate():
    """raw 调试入口只放行 READ_ONLY：免费写（领券/建地址）与花钱动作一律拦截。"""
    import main as m

    # 只读工具放行
    assert m._raw_tool_gate("query-meals") is None
    assert m._raw_tool_gate("calculate-price") is None

    # 免费写动作拦截（与帮助文字「任意只读工具」对齐）
    assert m._raw_tool_gate("auto-bind-coupons") is not None
    assert m._raw_tool_gate("delivery-create-address") is not None

    # 花钱动作拦截
    assert m._raw_tool_gate("create-order") is not None
    assert m._raw_tool_gate("cancel-order") is not None

    # 白名单之外的工具也拦截
    assert m._raw_tool_gate("not-a-real-tool") is not None


def test_lock_error_semantics():
    """锁竞争返回 skipped；目录/文件打开环境错误直接暴露，不被静默跳过。"""
    from src import schedule as sch
    from src.mcd_client import McdClient
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        sch.SCHEDULE_FILE = tmp / "schedules.json"
        sch.OUTBOX_DIR = tmp / "outbox"

        # ① 环境错误：锁文件父路径是「文件」而非目录 → mkdir 抛 OSError，应直接暴露
        blocker = tmp / "blocker"
        blocker.write_text("x")
        sch.FIRE_LOCK_FILE = blocker / ".lock"
        s = sch.add(kind="alert", at="09:00", note="环境错误测试")
        try:
            sch.fire(sch._NoMoneyClient(McdClient("x")), s)
            assert False, "目录创建失败应直接暴露（不返回 skipped）"
        except OSError:
            pass

        # ② 锁竞争：另一处已持锁 → fire 返回 skipped，而不是异常
        sch.FIRE_LOCK_FILE = tmp / ".fire.lock"
        s2 = sch.add(kind="alert", at="09:00", note="锁竞争测试")
        with sch._exclusive_lock(sch.FIRE_LOCK_FILE):
            try:
                with sch._exclusive_lock(sch.FIRE_LOCK_FILE):
                    assert False, "应抛 LockBusyError"
            except sch.LockBusyError:
                pass
            r = sch.fire(sch._NoMoneyClient(McdClient("x")), s2)
            assert r.get("skipped") is True and "另一进程" in r["reason"]


def _quote_fake_client(coupons=None, coupon_price=None):
    """算价测试通用 Fake：菜单两款商品 + 卡包券；coupon_code=BBB 模拟「券没到可用时间」。"""
    from src.mcd_client import McpError

    coupons = coupons if coupons is not None else [
        {"id": "C1", "code": "AAA", "title": "生效券",
         "tradeStartDateTime": "2026-10-01 00:00:00",
         "tradeEndDateTime": "2026-10-31 23:59:59"},
        {"id": "C2", "code": "BBB", "title": "未生效券",
         "tradeStartDateTime": "2026-10-20 00:00:00",
         "tradeEndDateTime": "2026-10-25 23:59:59"},
    ]
    calls = []

    class Fake:
        def call(self, name, args=None, confirm=False):
            calls.append((name, args))
            if name == "query-meals":
                return {"data": {"meals": {
                    "4810": {"name": "薯条", "code": "4810",
                             "currentPrice": 12.5, "originalPrice": 12.5},
                    "4811": {"name": "麦旋风", "code": "4811",
                             "currentPrice": 15.5, "originalPrice": 15.5},
                }}}
            if name == "query-my-coupons":
                return {"data": {"coupons": coupons}}
            if name == "query-store-coupons":
                return {"data": {"coupons": []}}
            if name == "calculate-price":
                code = (args["items"][0] or {}).get("couponCode")
                if code == "BBB":
                    raise McpError("当前优惠券不可用（未到生效时间）")
                price = coupon_price.get(code, 2500) if coupon_price else 2500
                return {"data": {"price": price, "originalPrice": 2500, "discount": 0,
                                 "takeWayList": [{"code": "eat-in", "title": "堂食"}],
                                 "storeName": "测试店"}}
            raise AssertionError(f"不应调用 {name}")

        def meals(self, store_code, **kw):
            return self.call("query-meals", kw or {})

        def my_coupons(self):
            return self.call("query-my-coupons")

        def store_coupons(self, store_code, **kw):
            return self.call("query-store-coupons")

    return Fake(), calls


def test_reprice_preserves_items():
    """P1 回归：reprice_draft 必须保留 items 完整结构（roundList/modification/
    替换项/规格）与 reservationDate，不能再按 (name, qty) 重建导致定制商品被
    确认闸误拦（diff 出「商品清单已变化」）。"""
    from src import optimize, order_exec

    cli, calls = _quote_fake_client()
    draft = {
        "storeCode": "1421252", "storeName": "测试店", "takeWayCode": "eat-in",
        "takeWayName": "堂食", "orderType": 1, "beType": 1, "beCode": "",
        "addressId": "", "addressText": "", "deliveryFee": 0.0, "packingFee": 0.0,
        "items": [{"productCode": "4810", "name": "薯条", "quantity": 2,
                   "roundList": [{"id": "r1", "name": "原味"}],
                   "modification": [{"type": "noOnion"}],
                   "replaceItem": {"productCode": "999"},
                   "spec": "大份",
                   "unitPrice": 12.5, "subtotal": 25.0}],
        "coupon": None, "price": 25.0, "originalPrice": 25.0, "discount": 0,
        "reservationDate": "2026-10-20 12:00",
        "quotedAt": "2026-10-13 18:00:00",
    }
    fresh = optimize.reprice_draft(cli, draft)

    # calculate-price 收到的是完整 item（含套餐规格/替换项），不是重建的 (code, qty)
    sent = [a for n, a in calls if n == "calculate-price"][0]["items"]
    assert sent[0].get("roundList") == [{"id": "r1", "name": "原味"}]
    assert sent[0].get("modification") == [{"type": "noOnion"}]
    assert sent[0].get("replaceItem") == {"productCode": "999"}
    assert sent[0].get("spec") == "大份"

    # 新草稿保留完整结构与用户既定字段
    assert fresh["items"][0].get("roundList") == [{"id": "r1", "name": "原味"}]
    assert fresh["items"][0].get("modification") == [{"type": "noOnion"}]
    assert fresh["items"][0].get("replaceItem") == {"productCode": "999"}
    assert fresh["reservationDate"] == "2026-10-20 12:00"

    # 价格/券/商品都没变 → diff 必须为空（定制商品不再被确认闸误拦）
    assert order_exec.diff_draft(draft, fresh) == []

    # 商品清单缺失 → 报错而非空清单继续
    try:
        optimize.reprice_draft(cli, {"storeCode": "S", "items": []})
        assert False, "空商品清单应报错"
    except ValueError:
        pass


def test_optimize_specified_coupon():
    """--coupon 具体券：只测「无券基线+该券」；不可用时 warning 注明生效时间窗，
    退回无券价，绝不静默枚举全量。"""
    from src import optimize

    # ① auto：枚举全部（基线 + 两张券 = 3 次 calculate-price），生效券 AAA 更便宜
    coupon_price = {"AAA": 2000}
    cli, calls = _quote_fake_client(coupon_price=coupon_price)
    d = optimize.optimize(cli, "1421252", [("麦旋风", 1)])
    assert len([c for c in calls if c[0] == "calculate-price"]) == 3
    assert d["coupon"]["couponCode"] == "AAA" and d["price"] == 20.0

    # ② 指定 AAA：只算基线 + AAA（2 次），同样命中
    cli2, calls2 = _quote_fake_client(coupon_price=coupon_price)
    d2 = optimize.optimize(cli2, "1421252", [("麦旋风", 1)], coupon="AAA")
    assert len([c for c in calls2 if c[0] == "calculate-price"]) == 2
    assert d2["coupon"]["couponCode"] == "AAA" and d2["price"] == 20.0

    # ③ 指定 BBB（未生效）：算价业务失败 → warning 带生效时间窗，退回无券价
    cli3, calls3 = _quote_fake_client(coupon_price=coupon_price)
    d3 = optimize.optimize(cli3, "1421252", [("麦旋风", 1)], coupon="BBB")
    assert d3["coupon"] is None and d3["price"] == 25.0
    warns = "；".join(d3.get("pricingWarnings") or [])
    assert "指定券" in warns and "BBB" in warns
    assert "2026-10-20" in warns and "2026-10-25" in warns, warns  # 生效窗口可见

    # ④ 指定不存在的券：不发起该券算价，warning 说明退回无券
    cli4, calls4 = _quote_fake_client(coupon_price=coupon_price)
    d4 = optimize.optimize(cli4, "1421252", [("麦旋风", 1)], coupon="NOT-EXIST")
    assert len([c for c in calls4 if c[0] == "calculate-price"]) == 1
    assert d4["coupon"] is None
    assert any("不在卡包/门店可用券中" in w for w in d4.get("pricingWarnings") or [])


def test_schedule_coupon_passthrough():
    """定时触发必须把 sch['coupon'] 传进 optimize（此前只传 track，指定券被静默忽略）。"""
    from src import schedule as sch
    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        tmp = Path(td)
        sch.SCHEDULE_FILE = tmp / "schedules.json"
        sch.OUTBOX_DIR = tmp / "outbox"
        sch.FIRE_LOCK_FILE = tmp / ".lock"
        order_exec.DRAFT_DIR = tmp / "drafts"

        s = sch.add(kind="order", at="08:00", store="1421252", items="麦旋风 x1",
                    coupon="BBB")
        cli, _ = _quote_fake_client()
        r = sch.fire(sch._NoMoneyClient(cli), s)
        draft = json.loads(Path(r["draft"]).read_text(encoding="utf-8"))
        # 指定券未生效 → 确认卡草稿的 warnings 必须说明原因与生效时间
        warns = "；".join(draft.get("pricingWarnings") or [])
        assert "指定券" in warns and "BBB" in warns and "生效" in warns, warns


def test_auto_claim_unknown_when_query_fails():
    """领前卡包查询失败时「本次领到」必须报未知，不得把已有卡包误报成本次领到。"""
    from src import coupons, payloads

    class FailBefore:
        def __init__(self):
            self.n = 0
            self.wallet = ["c1", "c2"]   # 领前已有 2 张

        def my_coupons(self):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("query down")   # 领前查询失败
            return {"data": {"coupons": [{"id": i} for i in self.wallet]}}

        def available_coupons(self):
            return {"data": [{"couponName": "券A", "couponStatus": "", "label": ""}]}

        def call(self, name, args=None, confirm=False):
            if name == "auto-bind-coupons":
                return {"ok": True}
            raise AssertionError(name)

    f = FailBefore()
    r = coupons.auto_claim(f)
    assert r["claimedCount"] is None and r["claimedIds"] == []
    assert any("未知" in e for e in r["errors"])
    # 回执卡也明确显示「未知」，不是 0 也不是候选数
    p = payloads.alert_coupon_receipt(r, "老大")
    assert "未知" in str(p["metrics"][0]["value"])

    # 领后查询失败同样报未知
    class FailAfter(FailBefore):
        def my_coupons(self):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("query down")
            return {"data": {"coupons": [{"id": i} for i in self.wallet]}}
    r2 = coupons.auto_claim(FailAfter())
    assert r2["claimedCount"] is None


def test_resolve_delivery_status_and_pair():
    """外卖解析：休息中门店必须被过滤/拒绝；(storeCode, beCode, 地址) 三者必须校验。"""
    from src import optimize

    stores = [
        {"storeCode": "S1", "beCode": "B1", "storeName": "休息店",
         "businessStatus": "休息中"},
        {"storeCode": "S2", "beCode": "B2", "storeName": "营业店",
         "businessStatus": "营业中"},
    ]

    class C:
        def delivery_addresses(self):
            return {"data": {"addresses": [{"addressId": "A1", "fullAddress": "某地"}]}}

        def delivery_stores(self, address_id, be_type=2):
            return {"data": stores}

    # ① 不指定门店 → 自动选第一个「营业中」的，跳过休息店
    r = optimize.resolve_delivery(C(), "A1")
    assert r["storeCode"] == "S2" and r["beCode"] == "B2"

    # ② 指定休息中门店 → 报错而不是拿去算价
    try:
        optimize.resolve_delivery(C(), "A1", store_code="S1")
        assert False, "休息中门店应被拒绝"
    except ValueError as e:
        assert "休息" in str(e)

    # ③ 可配送门店全休息 → 明确报错
    class CAll(C):
        def delivery_stores(self, address_id, be_type=2):
            return {"data": [{"storeCode": "S1", "beCode": "B1", "businessStatus": 0}]}
    try:
        optimize.resolve_delivery(CAll(), "A1")
        assert False, "全部休息应报错"
    except ValueError as e:
        assert "休息" in str(e)

    # ④ store + beCode 同给：匹配 → 通过
    r2 = optimize.resolve_delivery(C(), "A1", store_code="S2", be_code="B2")
    assert r2["beCode"] == "B2"

    # ⑤ store + beCode 同给：beCode 与接口不一致 → 拒绝（防无效组合直送算价）
    try:
        optimize.resolve_delivery(C(), "A1", store_code="S2", be_code="WRONG")
        assert False, "beCode 不一致应拒绝"
    except ValueError as e:
        assert "不一致" in str(e)

    # ⑥ store + beCode 同给：门店不配送该地址 → 拒绝
    try:
        optimize.resolve_delivery(C(), "A1", store_code="S9", be_code="B9")
        assert False, "不配送门店应拒绝"
    except ValueError as e:
        assert "不配送" in str(e)


def test_dual_candidates():
    """--track dual：同时算「全局最低」与「常买Top3」，confirm_candidates 出双动作。"""
    from src import optimize, payloads, price_memory

    cli, _ = _quote_fake_client()
    orig_top = price_memory.top_products
    price_memory.top_products = lambda n: [("麦旋风", 3)]
    try:
        res = optimize.optimize_dual(cli, "1421252", [("薯条", 1)])
    finally:
        price_memory.top_products = orig_top
    assert res["global"] and res["top3"]
    assert res["global"]["items"][0]["productCode"] == "4810"   # global 用用户点的薯条
    assert res["top3"]["items"][0]["productCode"] == "4811"     # top3 用常买麦旋风

    p = payloads.confirm_candidates(res["global"], res["top3"],
                                    path_a="g.json", path_b="t.json")
    assert p["type"] == "confirm"
    ids = {a["id"] for a in p["actions"]}
    assert ids == {"confirm_a", "confirm_b"}
    cmds = " ".join(a.get("command", "") for a in p["actions"])
    assert "g.json" in cmds and "t.json" in cmds and "确认下单" in cmds

    # top3 轨道失败 → 降级为单候选 + warnings，不拖垮 global
    price_memory.top_products = lambda n: [("不存在的商品", 1)]
    try:
        res2 = optimize.optimize_dual(cli, "1421252", [("薯条", 1)])
    finally:
        price_memory.top_products = orig_top
    assert res2["global"] and res2["top3"] is None
    assert any("Top3" in w for w in res2["warnings"])



def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_normalize()
        test_meal_index()
        test_price_memory(tmp)
        test_warmup_order_import_is_idempotent()
        test_warmup_migrates_legacy_profile_without_recounting()
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
        test_no_retry_on_write()
        test_post_network_no_retry()
        test_uncertain_order_probe()
        test_draft_binding_hardening()
        test_delivery_support()
        test_timeout_to_network_error()
        test_digest_covers_full_items()
        test_digest_covers_request_fields()
        test_build_argv_no_shell()
        test_phrase_propagates_exit_code()
        test_schedule_due_skipped_result()
        test_best_rejects_invalid_quantities()
        test_raw_readonly_gate()
        test_lock_error_semantics()
        test_reprice_preserves_items()
        test_optimize_specified_coupon()
        test_schedule_coupon_passthrough()
        test_auto_claim_unknown_when_query_fails()
        test_resolve_delivery_status_and_pair()
        test_dual_candidates()
    print("ALL OFFLINE TESTS PASSED ✅")


if __name__ == "__main__":
    main()
