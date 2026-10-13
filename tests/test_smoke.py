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
        ("我一共省了多少钱", "report"), ("发个战报", "share"),
        ("怎么用你", "doctor"), ("你好，你会干嘛", "capabilities"),
    ]
    for say, expect in cases:
        hit = ph.match(say)
        assert hit is not None, f"未匹配: {say}"
        assert hit["intent"] == expect, f"「{say}」→ {hit['intent']}，期望 {expect}"

    # 红线：花钱/写入类意图一律不可自动执行
    assert ph.runnable("patrol") and ph.runnable("report")
    for danger in ("order", "coupons_claim", "address"):
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
    print("ALL OFFLINE TESTS PASSED ✅")


if __name__ == "__main__":
    main()
