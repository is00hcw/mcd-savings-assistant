# -*- coding: utf-8 -*-
"""G · 定时执行 + 到点推送确认消息

能力：用户说「18:30 在金环宇店帮我下单麦旋风，用券」→ 登记定时任务 → 到点
自动**重新算价**（券可能过期、价可能变了）→ 推送 confirm 确认消息 → 用户回
「确认下单」才真正下单。

红线（最重要）：到点**绝不自动下单**。
  fire() 只做「重新算价 + 生成确认卡+ 落 outbox」，执行仍需 order --yes 的确认闸。
  这是本模块唯一允许的行为，不提供任何跳过确认的开关。

存储：~/.mcd-skill/schedules.json（任务）、~/.mcd-skill/outbox/（到点推送的确认卡）
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import CACHE_DIR
from . import payloads, present, persona

SCHEDULE_FILE = CACHE_DIR / "schedules.json"
OUTBOX_DIR = CACHE_DIR / "outbox"

KIND_ORDER = "order"   # 到点重算价 → 推送确认卡（不自动下单）
KIND_ALERT = "alert"   # 到点发一条提醒（纯文本/卡片，无副作用）


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _load() -> list[dict]:
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(items: list[dict]):
    SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 增删查
# ---------------------------------------------------------------------------
def add(kind: str = KIND_ORDER, at: str = "", store: str = "", items: str = "",
        track: str = "global", coupon: str = "auto", note: str = "",
        days: int = 0) -> dict:
    """登记定时任务。

    at  : "18:30"（每日到点）或 "2026-10-13 18:30"（指定日期到点）
    days: 连续重复天数（0=仅一次；>0 从今天起每天到点触发）
    """
    spec = parse_at(at)
    sch = {
        "id": "sch-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
        "kind": kind, "at": spec["raw"], "time": spec["time"], "date": spec["date"],
        "store": store, "items": items, "track": track, "coupon": coupon,
        "note": note, "repeatDays": days,
        "status": "pending", "createdAt": _now(), "firedAt": None,
    }
    items_all = _load()
    items_all.append(sch)
    _save(items_all)
    return sch


def remove(sid: str) -> bool:
    items = _load()
    rest = [s for s in items if s.get("id") != sid]
    if len(rest) == len(items):
        return False
    _save(rest)
    return True


def cancel(sid: str) -> bool:
    """取消（保留记录，状态置 cancelled，便于回溯）。"""
    items = _load()
    hit = False
    for s in items:
        if s.get("id") == sid:
            s["status"] = "cancelled"
            hit = True
    if hit:
        _save(items)
    return hit


def list_all(only_pending: bool = False) -> list[dict]:
    items = _load()
    if only_pending:
        items = [s for s in items if s.get("status") == "pending"]
    return sorted(items, key=lambda s: (s.get("time") or "", s.get("createdAt") or ""))


# ---------------------------------------------------------------------------
# 到点判定
# ---------------------------------------------------------------------------
def parse_at(at: str) -> dict:
    """解析 '18:30' / '2026-10-13 18:30' / '18:30:00'。"""
    raw = (at or "").strip()
    m = re.match(r"^(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?$", raw)
    if not m:
        raise ValueError(f"时间格式无法解析: {at!r}（应为 HH:MM 或 YYYY-MM-DD HH:MM）")
    date, hh, mm, ss = m.groups()
    if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
        raise ValueError(f"时间超出范围: {at!r}")
    return {"raw": raw, "date": date or None,
            "time": f"{int(hh):02d}:{int(mm):02d}:{int(ss or 0):02d}"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def due(schedules: list[dict] | None = None, now: datetime | None = None) -> list[dict]:
    """返回当前已到点、待触发的任务（含重复窗口）。"""
    now = now or datetime.now()
    out = []
    for s in (schedules if schedules is not None else list_all()):
        if s.get("status") != "pending":
            continue
        t = s.get("time") or "00:00:00"
        target = now.replace(hour=int(t[0:2]), minute=int(t[3:5]), second=int(t[6:8] or 0),
                             microsecond=0)
        if s.get("date"):  # 指定日期
            d = datetime.strptime(s["date"], "%Y-%m-%d")
            target = target.replace(year=d.year, month=d.month, day=d.day)
        if now >= target:
            out.append(s)
    return out


def next_trigger(s: dict, now: datetime | None = None) -> str:
    """人类可读的「下次触发」描述。"""
    now = now or datetime.now()
    t = s.get("time") or "00:00:00"
    hh, mm, ss = int(t[0:2]), int(t[3:5]), int(t[6:8] or 0)
    if s.get("date"):
        d = datetime.strptime(s["date"], "%Y-%m-%d")
        return f"{d.strftime('%m-%d')} {hh:02d}:{mm:02d}"
    if s.get("repeatDays"):
        return f"每天 {hh:02d}:{mm:02d}（连续 {s['repeatDays']} 天）"
    today = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if today > now:
        return f"今天 {hh:02d}:{mm:02d}"
    return f"明天 {hh:02d}:{mm:02d}（未触发）"


# ---------------------------------------------------------------------------
# 到点触发（红线：只推送确认消息，不下单）
# ---------------------------------------------------------------------------
class _NoMoneyClient:
    """到点触发的客户端外壳：**只允许只读调用**，其余一律拒绝（默认拒绝）。

    这是纵深防御：即使 optimize 或将来新增代码出现 bug、误调 create-order，
    或新增了 `create_order()` 这类便捷方法，也会在这里被拦住——
    从机制上保证「到点不会自动扣款」。
    """

    # 显式放行的只读便捷方法（内部一律转交 call()，从而统一受 MONEY_WRITE 白名单约束）
    READONLY_METHODS = ("meals", "my_coupons", "store_coupons", "available_coupons",
                        "order_list", "query_order", "now_time", "my_account",
                        "nearby_stores")

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        self.calls: list[str] = []

    def call(self, name, arguments=None, confirm=False):
        from .mcd_client import MONEY_WRITE, FREE_WRITE
        if name in MONEY_WRITE:
            raise PermissionError(
                f"红线拦截：到点触发阶段禁止调用 {name}（花钱动作）。"
                f"到点只推送确认消息，下单必须由用户确认后走 order --yes。")
        if name in FREE_WRITE:
            raise PermissionError(f"到点触发阶段禁止写操作：{name}")
        if confirm:
            raise PermissionError("红线拦截：到点触发阶段不允许带 confirm 调用任何工具。")
        self.calls.append(name)
        return self._inner.call(name, arguments, False)

    def __getattr__(self, item):
        # 只放行显式白名单里的只读便捷方法；其余（含未来的花钱便捷方法）直接拒绝
        if item in _NoMoneyClient.READONLY_METHODS:
            inner = object.__getattribute__(self, "_inner")
            return getattr(inner, item)
        raise AttributeError(
            f"红线拦截：到点触发阶段禁止使用客户端方法 {item!r}。"
            f"到点只允许只读查询与推送确认消息。")

    def __setattr__(self, key, value):
        if key in ("calls", "_inner"):
            object.__setattr__(self, key, value)
        else:
            raise AttributeError(f"到点触发阶段的客户端外壳不可设置 {key!r}")


def fire(client, sch: dict) -> dict:
    """到点执行：重算价 → 生成确认卡 → 落outbox。

    ⚠️ 绝不调用 create-order。执行仍需用户对 outbox 草稿回复确认后，
    走 `order --draft ... --yes --confirm-text "确认下单"`。
    """
    client = _NoMoneyClient(client)          # ← 硬隔离：到点阶段无写权限
    if sch.get("kind") == KIND_ALERT:
        p = payloads.alert_meal_time()
        r = _emit(sch, p, None)
        r["readOnlyCalls"] = client.calls
        return r

    from . import optimize, order_exec
    wanted = _parse_items(sch.get("items") or "")
    draft = optimize.optimize(client, sch["store"], wanted, track=sch.get("track") or "global")
    draft["scheduledFrom"] = {"id": sch["id"], "at": sch.get("at"), "note": sch.get("note")}
    draft["confirmedByHuman"] = False         # 显式标记：尚未获用户确认
    draft_path = order_exec.save_draft(draft)

    p = payloads.confirm_from_draft(draft, str(draft_path))
    p["title"] = f'到点了：{sch.get("note") or "该下单了"}'
    p["summary"] = (
        f'{persona.nick()}，{sch.get("at")} 的单我算好了：原价 {draft.get("originalPrice")}，'
        f'到手 {draft.get("price")}，省 {draft.get("discount")}。'
        f'（价格与券已按此刻重新计算）你确认我就下，不确认我不动。')
    p["meta"]["hint"] = (
        f'到点任务 {sch["id"]}｜{sch.get("note") or "无备注"}｜'
        f'券：{(draft.get("coupon") or {}).get("name") or "无可用券"}｜'
        f'⛔ 这条只是确认消息，我不会自动下单。')
    r = _emit(sch, p, draft_path)
    r["readOnlyCalls"] = client.calls
    return r


def _parse_items(s: str) -> list[tuple[str, int]]:
    out = []
    for part in (s or "").split(","):
        p = part.strip()
        if not p:
            continue
        if "x" in p.lower():
            name, qty = p.lower().split("x", 1)
            out.append((name.strip(), int(qty or 1)))
        else:
            out.append((p, 1))
    return out


def _emit(sch: dict, payload: dict, draft_path) -> dict:
    """写 outbox（供 WorkBuddy/定时任务读取推送），返回推送信息。"""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = OUTBOX_DIR / f"{ts}-{sch['id']}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = OUTBOX_DIR / f"{ts}-{sch['id']}.html"
    html_path.write_text(present.render_html(payload), encoding="utf-8")

    # 更新任务状态：pending → fired（等用户确认后才置 done）
    items = _load()
    for s in items:
        if s.get("id") == sch["id"]:
            s["status"] = "fired"
            s["firedAt"] = _now()
            s["outbox"] = {"json": str(json_path), "html": str(html_path),
                           "draft": str(draft_path) if draft_path else None}
    _save(items)
    return {"id": sch["id"], "payload": payload, "json": str(json_path),
            "html": str(html_path), "draft": str(draft_path) if draft_path else None}


def complete(sid: str) -> bool:
    """用户确认并下单成功后调用（由 order 命令回执联动，或手动标记）。"""
    items = _load()
    hit = False
    for s in items:
        if s.get("id") == sid:
            s["status"] = "done"
            hit = True
    if hit:
        _save(items)
    return hit


def describe(s: dict) -> str:
    kind = "下单确认" if s.get("kind") == KIND_ORDER else "到点提醒"
    what = (f'{s.get("store") or "（未指定门店）"}｜{s.get("items") or "（未指定商品）"}'
            if s.get("kind") == KIND_ORDER else (s.get("note") or "到点提醒"))
    return (f'[{s["id"]}] {kind}｜{s.get("at")}｜{what}'
            f'｜状态 {s.get("status")}｜{next_trigger(s)}')