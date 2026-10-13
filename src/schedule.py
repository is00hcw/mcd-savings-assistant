# -*- coding: utf-8 -*-
"""G · 定时执行 + 到点推送确认消息

能力：用户说「18:30 在金环宇店帮我下单麦旋风，用券」→ 登记定时任务 → 到点
自动**重新算价**（券可能过期、价可能变了）→ 推送 confirm 确认消息 → 用户回
「确认下单」才真正下单。

产品定位（重要）：定时任务 = 「到点重算价 + 提醒 + 人工确认」的**即时单**，
不做预约单。--at 只是触发算价/提醒的时间；到点确认后按**当下**价格下单、
当下取餐。草稿的 reservationDate 恒为 None（字段仅为兼容 create-order 的
预约能力保留）。若要"预约未来取餐"，请到 App 操作或另行明确定义。

红线（最重要）：到点**绝不自动下单**。
  fire() 只做「重新算价 + 生成确认卡 + 落 outbox」，执行仍需 order --yes 的确认闸。
  这是本模块唯一允许的行为，不提供任何跳过确认的开关。

存储：~/.mcd-skill/schedules.json（任务）、~/.mcd-skill/outbox/（到点推送的确认卡）
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .config import CACHE_DIR
from . import payloads, present

SCHEDULE_FILE = CACHE_DIR / "schedules.json"
OUTBOX_DIR = CACHE_DIR / "outbox"
FIRE_LOCK_FILE = CACHE_DIR / ".schedule-fire.lock"

RETRY_AFTER_SECONDS = 5 * 60   # 触发失败后的重试抑制窗口（避免同分钟反复补触发）

KIND_ORDER = "order"   # 到点重算价 → 推送确认卡（不自动下单）
KIND_ALERT = "alert"   # 到点发一条提醒（纯文本/卡片，无副作用）


class LockBusyError(Exception):
    """锁被另一进程持有（真正的锁竞争）。

    与「锁文件打不开 / 目录无权限」等环境错误严格区分：环境错误应直接暴露，
    不能被当成「另一进程正在触发」而静默跳过（否则权限异常会让任务被悄悄漏掉）。
    """


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


@contextmanager
def _exclusive_lock(lock_path: Path):
    """跨进程互斥锁（Windows msvcrt / Unix flock）。

    保护「到点判断 → claim → 触发」的状态流转：两个同时运行的定时任务
    进程不会对同一条任务各生成一份确认草稿。

    失败语义严格区分：
      · 锁竞争（另一进程已持锁）→ 抛 LockBusyError（fire 层跳过本次）；
      · 目录创建 / 文件打开失败 → 原样抛 OSError（不应静默跳过）。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)   # 环境错误：直接暴露
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)  # 环境错误：直接暴露
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, 0)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # 非阻塞：拿不到抛 OSError
            except OSError:
                raise LockBusyError("另一进程正在触发定时任务") from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise LockBusyError("另一进程正在触发定时任务") from None
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _get(sid: str) -> dict | None:
    for s in _load():
        if s.get("id") == sid:
            return s
    return None


def _mark(sid: str, **fields) -> dict | None:
    """更新单条任务的部分字段并保存，返回更新后的任务。"""
    items = _load()
    hit = None
    for s in items:
        if s.get("id") == sid:
            s.update(fields)
            hit = s
    _save(items)
    return hit


def _after_seconds(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


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
        "id": "sch-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"),
        "kind": kind, "at": spec["raw"], "time": spec["time"], "date": spec["date"],
        "store": store, "items": items, "track": track, "coupon": coupon,
        "note": note, "repeatDays": days,
        "status": "pending", "createdAt": _now(), "firedAt": None,
        "fireCount": 0, "lastFiredDate": None,
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


def _today_str(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _day_of(created_at: str | None) -> "datetime.date":
    """从 createdAt 字符串取日期（用于重复任务的起始日）。"""
    try:
        return datetime.strptime(str(created_at or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return datetime.now().date()


def due(schedules: list[dict] | None = None, now: datetime | None = None) -> list[dict]:
    """返回当前已到点、待触发的任务（含重复窗口）。

    判定规则：
      · 指定日期（date）：到点一次性触发。
      · 连续 repeatDays 天：从登记当天（第 1 天）起，每天到点触发一次，
        当天去重（lastFiredDate），触发满 repeatDays 次后不再出现。
      · 仅一次（repeatDays=0）：今天到点且尚未触发过。
    """
    now = now or datetime.now()
    out = []
    for s in (schedules if schedules is not None else list_all()):
        if s.get("status") != "pending":
            continue
        # 失败重试抑制：上次触发失败后，nextRetryAt 之前不再补触发
        nra = s.get("nextRetryAt")
        if nra:
            try:
                if datetime.strptime(str(nra)[:19], "%Y-%m-%d %H:%M:%S") > now:
                    continue
            except ValueError:
                pass
        t = s.get("time") or "00:00:00"
        hh, mm = int(t[0:2]), int(t[3:5])
        ss = int(t[6:8] or 0)

        if s.get("date"):  # 指定日期：一次性
            d = datetime.strptime(s["date"], "%Y-%m-%d")
            target = d.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            if now >= target:
                out.append(s)
            continue

        today_target = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if now < today_target:
            continue  # 今天还没到点

        rd = int(s.get("repeatDays") or 0)
        if rd > 0:
            start = _day_of(s.get("createdAt"))
            day_index = (now.date() - start).days + 1
            if day_index > rd:
                continue  # 已超出连续天数
            if s.get("lastFiredDate") == _today_str(now):
                continue  # 今天已触发过，去重
            out.append(s)
        else:
            if int(s.get("fireCount") or 0) == 0:
                out.append(s)
    return out


def next_trigger(s: dict, now: datetime | None = None) -> str:
    """人类可读的「下次触发」描述（与 due() 的判定口径保持一致）。"""
    now = now or datetime.now()
    t = s.get("time") or "00:00:00"
    hh, mm = int(t[0:2]), int(t[3:5])
    ss = int(t[6:8] or 0)
    if s.get("date"):
        d = datetime.strptime(s["date"], "%Y-%m-%d")
        target = d.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if now < target:
            return f"{d:%m-%d} {hh:02d}:{mm:02d}"
        return "已到点，待触发" if s.get("status") == "pending" else "已触发"
    rd = int(s.get("repeatDays") or 0)
    if rd > 0:
        n = int(s.get("fireCount") or 0)
        return f"每天 {hh:02d}:{mm:02d}（连续 {rd} 天，已触发 {n}/{rd}）"
    today = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if today > now:
        return f"今天 {hh:02d}:{mm:02d}"
    if s.get("status") == "pending":
        return f"已到点，待触发（{hh:02d}:{mm:02d}）"
    return f"{hh:02d}:{mm:02d}（已触发）"


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

    并发安全：持文件锁 + 锁内 claim（pending→firing），两个同时运行的
    定时任务进程不会对同一条任务各生成一份确认草稿。
    """
    try:
        with _exclusive_lock(FIRE_LOCK_FILE):
            return _fire_locked(client, sch)
    except LockBusyError:
        return {"skipped": True, "id": sch.get("id"),
                "reason": "另一进程正在触发定时任务，本次跳过"}
    # 目录创建 / 文件打开失败等环境错误（OSError）不在此捕获：直接向上暴露，
    # 避免权限异常被当成「锁竞争」而静默漏触发。


def _fire_locked(client, sch: dict) -> dict:
    """持锁下的触发：先 claim 防并发，失败回滚 pending 并抑制重试。"""
    # 锁内重读最新状态：并发下另一个进程可能已触发/已 claim
    current = _get(sch["id"])
    if current is None:
        return {"skipped": True, "id": sch["id"], "reason": "任务不存在"}
    if current.get("status") != "pending":
        return {"skipped": True, "id": sch["id"],
                "reason": f"任务已处于 {current.get('status')} 状态，跳过"}
    nra = current.get("nextRetryAt")
    if nra:
        try:
            if datetime.strptime(str(nra)[:19], "%Y-%m-%d %H:%M:%S") > datetime.now():
                return {"skipped": True, "id": sch["id"], "reason": "上次失败，等待重试窗口"}
        except ValueError:
            pass
    # claim：pending → firing（占位，防并发重复触发）
    _mark(sch["id"], status="firing", claimedAt=_now())
    sch = _get(sch["id"]) or current

    client = _NoMoneyClient(client)          # ← 硬隔离：到点阶段无写权限
    try:
        if sch.get("kind") == KIND_ALERT:
            items = [{"name": n, "quantity": q, "note": f"待下单，需你确认"}
                     for n, q in _parse_items(sch.get("items") or "")]
            p = payloads.alert_meal_time(items=items, sch=sch)
            r = _emit(sch, p, None)
            r["readOnlyCalls"] = client.calls
            return r

        from . import optimize, order_exec
        wanted = _parse_items(sch.get("items") or "")
        # --coupon 语义与 CLI 声明一致：auto=枚举所有候选券；具体券 id/code=只测
        # 「无券基线+该券」（不可用时 optimize 记 warning 并注明生效时间窗，
        # 退回无券价，绝不静默忽略用户指定的券）
        draft = optimize.optimize(client, sch["store"], wanted,
                                  track=sch.get("track") or "global",
                                  coupon=sch.get("coupon") or "auto")
        draft["scheduledFrom"] = {"id": sch["id"], "at": sch.get("at"),
                                  "note": sch.get("note")}
        draft["confirmedByHuman"] = False         # 显式标记：尚未获用户确认
        draft_path = order_exec.save_draft(draft)

        # 标题 / 话术 / 红线声明统一由 confirm_from_draft 依据 scheduledFrom 生成，
        # 这里不再手工拼 summary/hint（避免两处文案漂移）。
        p = payloads.confirm_from_draft(draft, str(draft_path))
        r = _emit(sch, p, draft_path)
        r["readOnlyCalls"] = client.calls
        return r
    except Exception as e:
        # 失败回滚：保持 pending + 记录错误 + nextRetryAt 抑制重试
        # （避免「算价失败→仍 pending→下一轮立即补触发」造成重复提醒）
        _mark(sch["id"], status="pending", lastError=str(e),
              lastErrorAt=_now(), nextRetryAt=_after_seconds(RETRY_AFTER_SECONDS))
        raise


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
            s["fireCount"] = int(s.get("fireCount") or 0) + 1
            s["lastFiredDate"] = datetime.now().strftime("%Y-%m-%d")
            s["firedAt"] = _now()
            s["outbox"] = {"json": str(json_path), "html": str(html_path),
                           "draft": str(draft_path) if draft_path else None}
            # 成功触发：清掉 claim 占位与上次失败的痕迹
            s["claimedAt"] = None
            s["lastError"] = None
            s["lastErrorAt"] = None
            s["nextRetryAt"] = None
            rd = int(s.get("repeatDays") or 0)
            # 还有剩余天数 → 保持 pending 等下一次；否则置 fired 收尾
            s["status"] = "pending" if (rd > 0 and s["fireCount"] < rd) else "fired"
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