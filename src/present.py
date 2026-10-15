# -*- coding: utf-8 -*-
"""14.3 输出展示形态规范 · 结构化输出层

原则（同数据源、多渲染）：
  ① 核心逻辑只产出**结构化数据**（payload），带展示意图 type:
     alert | confirm | report | share；渲染层可插拔。
  ② 组件优先于 HTML 文件：90% 场景是"快速提醒 + 快速确认" → 内联卡片。
  ③ 跨宿主降级：WorkBuddy 内渲染交互组件；纯 CLI / 其他 AI 助手降级 Markdown。
  ④ 图片用于分享、不用于交互：待下单方案卡出图片；确认闸绝不用图片。

场景 → 形态映射（方案 14.3 表）：
  降价提醒 / 领券回执 / 到点提醒 → alert  → 内联卡片
  下单确认（确认闸）             → confirm → 交互卡片（确认/取消）
  省钱账单（屏内细看/存档）        → report  → HTML 页面
  待下单优惠方案（对外分享）      → share   → 图片（SVG）
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1

TYPE_ALERT = "alert"
TYPE_CONFIRM = "confirm"
TYPE_REPORT = "report"
TYPE_SHARE = "share"
VALID_TYPES = (TYPE_ALERT, TYPE_CONFIRM, TYPE_REPORT, TYPE_SHARE)

# type → 推荐渲染形态（渲染层据此选路；宿主不支持则按 14.3 原则③降级）
PREFERRED_RENDER = {
    TYPE_ALERT: "inline",
    TYPE_CONFIRM: "inline",
    TYPE_REPORT: "html",
    TYPE_SHARE: "image",
}

ICON = {
    TYPE_ALERT: "🔔",
    TYPE_CONFIRM: "🧾",
    TYPE_REPORT: "📊",
    TYPE_SHARE: "🏆",
}


# ---------------------------------------------------------------------------
# ① payload 构造（同数据源）
# ---------------------------------------------------------------------------
def make_payload(type_: str, title: str, summary: str = "",
                 metrics: list[dict] | None = None,
                 items: list[dict] | None = None,
                 actions: list[dict] | None = None,
                 draft: dict | None = None,
                 recipe: dict | None = None,
                 meta: dict | None = None,
                 ts: str | None = None) -> dict[str, Any]:
    """构造统一 payload。所有业务模块只调它，不直接拼字符串。"""
    if type_ not in VALID_TYPES:
        raise ValueError(f"type 必须是 {VALID_TYPES} 之一，收到: {type_}")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "type": type_,
        "preferredRender": PREFERRED_RENDER[type_],
        "ts": ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": title,
        "summary": summary,
        "metrics": metrics or [],
        "items": items or [],
        "actions": actions or [],
        "draft": draft,
        "recipe": recipe,
        "meta": meta or {},
    }


# ---------------------------------------------------------------------------
# ② 内联卡片渲染（WorkBuddy 对话流内直接渲染；CLI 降级为同一份 Markdown）
# ---------------------------------------------------------------------------
def render_inline(p: dict) -> str:
    """内联卡片 → Markdown 文本。

    WorkBuddy 宿主读到本 payload 可渲染为富卡片；其他宿主直接显示此文本。
    """
    ic = ICON.get(p["type"], "")
    out = [f"**{ic} {p['title']}**"]

    if p["summary"]:
        out.append(p["summary"])

    if p["metrics"]:
        for m in p["metrics"]:
            val = m.get("value")
            unit = m.get("unit", "")
            label = m.get("label", "")
            if m.get("emphasis"):
                out.append(f"- **{val}{unit}**　{label}")
            else:
                out.append(f"- {val}{unit}　{label}")

    if p["items"]:
        out.append("")
        for it in p["items"]:
            name = it.get("name") or it.get("label") or ""
            qty = it.get("quantity")
            price = it.get("price")
            note = it.get("note") or it.get("detail") or ""
            line = f"- {name}"
            if qty:
                line += f" × {qty}"
            if price not in (None, ""):
                line += f"　¥{price:g}" if isinstance(price, (int, float)) else f"　¥{price}"
            if note:
                line += f"（{note}）"
            out.append(line)

    if p["actions"]:
        out.append("")
        for a in p["actions"]:
            # command 是给 Agent 执行的指令，不展示给终端用户（太长、暴露路径、含 --yes 凭据）
            out.append(f"[{a.get('label')}]")

    if p["meta"].get("hint"):
        out.append("")
        out.append(f"> {p['meta']['hint']}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# ③ HTML 页面渲染（report 账单细看 / 存档；share 方案也可用）
# ---------------------------------------------------------------------------
_HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title}</title>
<style>
 body{{margin:0;background:#f6f7f9;color:#1d2129;
   font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.7}}
 .wrap{{max-width:760px;margin:0 auto;padding:28px 20px 60px}}
 header{{background:linear-gradient(135deg,#ffc600,#ffb000);border-radius:16px;padding:22px 26px;color:#3a2c00}}
 header h1{{margin:0 0 4px;font-size:22px}} header .ts{{font-size:12px;opacity:.75}}
 .sum{{margin:16px 0 0;font-size:15px;font-weight:600}}
 .metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:18px 0}}
 .m{{background:#fff;border:1px solid #e6e8eb;border-radius:12px;padding:14px 16px}}
 .m .v{{font-size:26px;font-weight:800;color:#e4002b}} .m .v small{{font-size:13px;font-weight:500;color:#5c6573}}
 .m .l{{font-size:12px;color:#5c6573;margin-top:2px}}
 table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;
   border:1px solid #e6e8eb}}
 th,td{{padding:11px 14px;text-align:left;font-size:14px;border-bottom:1px solid #eef0f2}}
 th{{background:#fafbfc;color:#5c6573;font-weight:600;font-size:13px}}
 tr:last-child td{{border-bottom:none}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
 .sec{{margin:22px 0 10px;font-size:15px;font-weight:700}}
 .note{{margin-top:18px;padding:12px 14px;background:#fffbe6;border:1px solid #ffe08a;
   border-radius:10px;font-size:13px;color:#6b5b1f}}
 .recipe{{margin-top:12px;background:#f2f3f5;border-radius:10px;padding:12px 14px;
   font-size:12px;color:#5c6573;word-break:break-all}}
 .foot{{margin-top:22px;text-align:center;font-size:11px;color:#a0a4ad}}
</style></head><body><div class="wrap">
<header><h1>{icon} {title}</h1><div class="ts">{ts}</div></header>
{summary_block}
{metrics_block}
{items_block}
{note_block}
{recipe_block}
<div class="foot">麦麦省钱助理 · 你的省钱盯价闹钟（数据只存本地，不收集不上传）</div>
</div></body></html>"""


def render_html(p: dict) -> str:
    summary_block = f'<div class="sum">{_esc(p["summary"])}</div>' if p["summary"] else ""

    metrics_block = ""
    if p["metrics"]:
        cells = "".join(
            f'<div class="m"><div class="v">{_esc(str(m.get("value", "")))}'
            f'<small>{_esc(m.get("unit", ""))}</small></div>'
            f'<div class="l">{_esc(m.get("label", ""))}</div></div>'
            for m in p["metrics"]
        )
        metrics_block = f'<div class="metrics">{cells}</div>'

    items_block = ""
    if p["items"]:
        cols = _item_columns(p["items"][0])
        head = "".join(f"<th>{_esc(c[1])}</th>" for c in cols)
        rows = []
        for it in p["items"]:
            tds = []
            for key, _label, numeric in cols:
                v = it.get(key, "")
                if key == "price" and v not in ("", None):
                    v = f"¥{v}"
                tds.append(f'<td class="num">{_esc(str(v))}</td>' if numeric
                           else f"<td>{_esc(str(v))}</td>")
            rows.append("<tr>" + "".join(tds) + "</tr>")
        items_block = ('<div class="sec">明细</div><table><thead><tr>'
                       + head + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    hint = p["meta"].get("hint")
    note_block = f'<div class="note">{_esc(hint)}</div>' if hint else ""

    recipe_block = ""
    if p.get("recipe"):
        recipe_block = ('<div class="sec">order recipe（可复现配方，不含任何凭据）</div>'
                        f'<div class="recipe">{_esc(json.dumps(p["recipe"], ensure_ascii=False))}</div>')
    return _HTML_TMPL.format(
        title=_esc(p["title"]), icon=ICON.get(p["type"], ""), ts=_esc(p["ts"]),
        summary_block=summary_block, metrics_block=metrics_block,
        items_block=items_block, note_block=note_block, recipe_block=recipe_block,
    )


def _item_columns(sample: dict) -> list[tuple[str, str, bool]]:
    cols = [("name", "商品/项目", False)]
    for k, label in (("storeCode", "门店"), ("quantity", "数量", )):
        pass
    if sample.get("storeCode"):
        cols.append(("storeCode", "门店", False))
    if sample.get("quantity") is not None:
        cols.append(("quantity", "数量", True))
    if sample.get("originalPrice") is not None:
        cols.append(("originalPrice", "原价", True))
    if sample.get("price") is not None:
        cols.append(("price", "到手价", True))
    if sample.get("note") or sample.get("detail"):
        cols.append(("note", "说明", False))
    return cols


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# ④ 图片渲染（share 方案 → SVG，零依赖、可保存可转发）
# ---------------------------------------------------------------------------
def render_svg(p: dict, width: int = 640) -> str:
    """优惠方案卡 → SVG 图片。

    图片用于分享、不用于交互（14.3 原则④）：不含任何可点确认元素。
    配方过长（二维码容量不足）时自动降级为「图片不带码」，仍可正常转发。
    """
    W = width
    pad = 28
    metrics = p["metrics"][:3]
    items = p["items"][:6]
    recipe = p.get("recipe")

    # ---- 正文行（先量高度） ----
    rows: list[str] = []
    y = 128 if metrics else 108
    for m in metrics:
        rows.append(
            f'<text x="{pad}" y="{y}" font-size="13" fill="#5c6573">{_esc(m.get("label", ""))}</text>'
            f'<text x="{pad}" y="{y + 24}" font-size="24" font-weight="bold" fill="#e4002b">'
            f'{_esc(str(m.get("value", "")))}'
            f'<tspan font-size="13" font-weight="normal" fill="#5c6573">'
            f'{_esc(m.get("unit", ""))}</tspan></text>')
        y += 50
    if items:
        rows.append(f'<line x1="{pad}" y1="{y - 16}" x2="{W - pad}" y2="{y - 16}" '
                    f'stroke="#eceef1" stroke-width="1"/>')
        for it in items:
            nm = str(it.get("name") or "")
            if it.get("quantity"):
                nm += f' × {it["quantity"]}'
            pr = f'¥{it["price"]}' if it.get("price") not in (None, "") else ""
            rows.append(
                f'<text x="{pad}" y="{y + 8}" font-size="14" fill="#1d2129">{_esc(nm)}</text>'
                f'<text x="{W - pad}" y="{y + 8}" font-size="14" font-weight="bold" '
                f'text-anchor="end" fill="#1d2129">{_esc(pr)}</text>')
            y += 30
    body_bottom = y

    # ---- 二维码区 ----
    qr_block = ""
    qr_h = 0
    if recipe:
        try:
            from .qr import qr_matrix
            module = 5
            compact = json.dumps(recipe, ensure_ascii=False, separators=(",", ":"))
            mm = qr_matrix(compact)
            n = len(mm)
            size = n * module + 8 * module
            qx, qy = W - pad - size, body_bottom + 18
            seg = [f'<rect x="{qx}" y="{qy}" width="{size}" height="{size}" fill="#fff" rx="6"/>',
                   '<path fill="#1d2129" d="']
            for r in range(n):
                for c in range(n):
                    if mm[r][c]:
                        x0, y0 = qx + (c + 4) * module, qy + (r + 4) * module
                        seg.append(f"M{x0} {y0}h{module}v{module}h-{module}z")
            seg.append('"/>')
            seg.append(f'<text x="{pad}" y="{qy + size / 2:.0f}" font-size="12" fill="#5c6573">'
                       f'扫码或复制配方即可复现这单</text>')
            seg.append(f'<text x="{pad}" y="{qy + size / 2 + 18:.0f}" font-size="12" fill="#c0392b">'
                       f'（须你本人确认后才会下单）</text>')
            qr_block = "".join(seg)
            qr_h = size + 18
        except Exception:
            qr_block = ""

    total_h = body_bottom + qr_h + 56

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {total_h}" width="{W}" height="{total_h}" font-family="-apple-system,'PingFang SC','Microsoft YaHei',sans-serif">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="#ffc600"/><stop offset="100%" stop-color="#ffb000"/></linearGradient></defs>
<rect width="{W}" height="{total_h}" rx="18" fill="url(#bg)"/>
<rect x="4" y="4" width="{W - 8}" height="{total_h - 8}" rx="15" fill="#fffdf5"/>
<text x="{pad}" y="46" font-size="20" font-weight="bold" fill="#3a2c00">{ICON.get(p["type"], "")} {_esc(p["title"])}</text>
<text x="{pad}" y="68" font-size="12" fill="#a08d3f">{_esc(p["ts"])}</text>
<text x="{pad}" y="90" font-size="13" fill="#5c6573">{_esc(p["summary"])}</text>
{"".join(rows)}
{qr_block}
<text x="{pad}" y="{total_h - 20}" font-size="11" fill="#b0a880">麦麦省钱助理 · 你只管吃，省钱它来盯</text>
</svg>"""


# ---------------------------------------------------------------------------
# 渲染分发（渲染层可插拔的唯一入口）
# ---------------------------------------------------------------------------
def validate_payload(p: dict) -> dict:
    """校验 payload 结构与必填字段，不合法抛 ValueError。

    外部契约校验：type 合法 + title/schemaVersion 必填 +
    metrics/items/actions 为 list + 每个 action 含稳定的 id 与 label。
    （command 是给宿主/Agent 的可执行指令，不作为按钮直接可用。）
    """
    if not isinstance(p, dict):
        raise ValueError("payload 必须是 dict")
    t = p.get("type")
    if t not in VALID_TYPES:
        raise ValueError(f"payload type 非法: {t!r}，应为 {VALID_TYPES} 之一")
    if not p.get("title"):
        raise ValueError("payload 缺少必填字段 title")
    if "schemaVersion" not in p:
        raise ValueError("payload 缺少必填字段 schemaVersion")
    for field in ("metrics", "items", "actions"):
        v = p.get(field)
        if v is not None and not isinstance(v, list):
            raise ValueError(f"payload.{field} 必须是 list")
    for a in (p.get("actions") or []):
        if not isinstance(a, dict) or not a.get("id") or not a.get("label"):
            raise ValueError("payload.actions[] 必须含稳定的 id 与 label")
    return p


def render(p: dict, fmt: str = "auto") -> dict:
    """按 fmt 渲染 payload。

    fmt: auto（按 preferredRender）| inline | json | html | svg | md
    返回 {"format": 实际格式, "content": 内容, "payload": payload}
    auto 在 CLI 下默认降级为 inline（原则③：任何宿主都能渲染）。
    """
    validate_payload(p)
    if fmt == "auto":
        fmt = p.get("preferredRender") or "inline"
    if fmt in ("inline", "md"):
        return {"format": "inline", "content": render_inline(p), "payload": p}
    if fmt == "json":
        return {"format": "json",
                "content": json.dumps(p, ensure_ascii=False, indent=2), "payload": p}
    if fmt == "html":
        return {"format": "html", "content": render_html(p), "payload": p}
    if fmt in ("svg", "image"):
        return {"format": "svg", "content": render_svg(p), "payload": p}
    raise ValueError(f"未知渲染格式: {fmt}")


def emit(p: dict, fmt: str = "auto", out_path: str | None = None) -> dict:
    """渲染并落地：out_path 给了就写文件（HTML/SVG 产物），返回渲染结果。"""
    r = render(p, fmt)
    if out_path:
        import pathlib
        pth = pathlib.Path(out_path)
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(r["content"], encoding="utf-8")
        r["path"] = str(pth.resolve())
    return r
