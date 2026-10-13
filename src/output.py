# -*- coding: utf-8 -*-
"""14.3 输出展示形态规范 · 结构化输出层

核心原则（产品方案 v0.15 §14.3）：
① 同数据源多渲染：业务逻辑只产出结构化消息（含展示意图 type），渲染层可插拔。
② 组件优先于 HTML 文件：90% 场景是"快速提醒+快速确认"→ 内联卡片；账单才用 HTML 整页。
③ 跨宿主降级：WorkBuddy 宿主渲染组件；纯 CLI / 其他 AI 助手降级 Markdown
   （alert→加粗文本，confirm→带 [确认]/[取消] 的文本提示，share→输出图片文件路径）。
④ 图片用于分享、不用于交互：确认下单绝不靠图片。

type 四类：
  alert   降价提醒 / 领券回执 / 到点提醒 / 临期积分   → 内联卡片（CLI: 加粗文本）
  confirm 下单确认闸（草稿 + [确认]/[取消]）          → 交互卡片（CLI: 文本提示）
  report  省钱账单 / 巡检汇总（屏内细看）              → HTML 整页 / 富卡片（CLI: Markdown 表格）
  share   省钱战报（对外转发）                        → 图片文件（CLI: 输出文件路径）
"""
import json
from datetime import datetime

from . import share_card


# ---------------------------------------------------------------------------
# 构造器（业务层只调这些）
# ---------------------------------------------------------------------------
def alert(title: str, lines: list[str], metrics: list[dict] | None = None,
          action: str = "", **extra) -> dict:
    """快速看一眼的消息。metrics: [{"label":"省","value":"4.5","highlight":True}]"""
    return {"type": "alert", "title": title, "lines": lines,
            "metrics": metrics or [], "action": action, **extra}


def confirm(title: str, draft: dict, question: str | None = None) -> dict:
    """确认闸消息：data 必须可追溯（内嵌完整 draft）。"""
    coupon = draft.get("coupon") or {}
    lines = [
        f"门店：{draft.get('storeName', draft.get('storeCode'))} · {draft.get('takeWayName') or draft.get('takeWayCode')}",
        f"商品：{'、'.join(f\"{it.get('name') or it.get('productCode')}×{it.get('quantity')}\" for it in draft.get('items', []))}",
        f"用券：{coupon.get('name') or '无'}",
        draft.get("savingDetail", ""),
    ]
    return {
        "type": "confirm", "title": title,
        "question": question or "你确认我就下，不确认我不动。",
        "choices": ["确认下单", "取消"],
        "price": draft.get("price"), "originalPrice": draft.get("originalPrice"),
        "discount": draft.get("discount"),
        "draftPath": draft.get("_draftPath", ""),
        "lines": lines,
        "draft": draft,
    }


def report(title: str, sections: list[dict], summary: list[dict] | None = None) -> dict:
    """细看/存档消息。sections: [{"heading":str,"table":{"head":[],"rows":[[]]}} or {"lines":[str]}]"""
    return {"type": "report", "title": title, "summary": summary or [], "sections": sections}


def share(title: str, image_path: str, recipe_prompt: str, saved: float = 0) -> dict:
    """分享消息：载体是图片文件，绝不承载交互。"""
    return {"type": "share", "title": title, "imagePath": image_path,
            "recipePrompt": recipe_prompt, "saved": saved}


# ---------------------------------------------------------------------------
# 渲染层 1：Markdown（CLI / 任意宿主兜底，原则③）
# ---------------------------------------------------------------------------
def to_markdown(msg: dict) -> str:
    t = msg.get("type")
    out = []
    if t == "alert":
        out.append(f"**🔔 {msg['title']}**")
        for m in msg.get("metrics", []):
            hl = "**" if m.get("highlight") else ""
            out.append(f"- {m['label']}：{hl}{m['value']}{hl}")
        for ln in msg.get("lines", []):
            out.append(f"- {ln}")
        if msg.get("action"):
            out.append(f"\n👉 {msg['action']}")
    elif t == "confirm":
        out.append(f"**⚠️ {msg['title']}**")
        for ln in msg.get("lines", []):
            if ln:
                out.append(f"- {ln}")
        try:
            price, orig = float(msg.get("price") or 0), float(msg.get("originalPrice") or 0)
            disc = float(msg.get("discount") or 0)
            out.append(f"- **合计：¥{price:g}（原价 ¥{orig:g}，省 ¥{disc:g}）**")
        except (TypeError, ValueError):
            pass
        out.append(f"\n{msg.get('question', '')}")
        if msg.get("draftPath"):
            out.append(f"确认执行：`python main.py order --draft \"{msg['draftPath']}\" --yes --confirm-text \"确认下单\"`")
        out.append("**[确认下单] / [取消]**（未确认不会执行任何花钱操作）")
    elif t == "report":
        out.append(f"**📊 {msg['title']}**")
        for s in msg.get("summary", []):
            out.append(f"- {s}")
        for sec in msg.get("sections", []):
            out.append(f"\n**{sec.get('heading', '')}**")
            if sec.get("lines"):
                out.extend(f"- {ln}" for ln in sec["lines"])
            tab = sec.get("table")
            if tab:
                head = tab.get("head", [])
                out.append("| " + " | ".join(head) + " |")
                out.append("|" + "---|" * len(head))
                for row in tab.get("rows", []):
                    out.append("| " + " | ".join(str(c) for c in row) + " |")
    elif t == "share":
        out.append(f"**📣 {msg['title']}**")
        out.append(f"- 战报图：`{msg.get('imagePath', '')}`")
        out.append(f"- 好友复现提示词：\n\n> {msg.get('recipePrompt', '')}")
    else:
        out.append(f"**{msg.get('title', t or '')}**")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 渲染层 2：内联卡片 HTML 片段（对话流内直接渲染，原则②）
# ---------------------------------------------------------------------------
_CARD_CSS = (
    "display:block;max-width:460px;margin:8px 0;padding:12px 16px;border-radius:12px;"
    "font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
    "font-size:14px;line-height:1.7;border:1px solid #e6e8eb;background:#fffdf5;color:#1d2129;"
)


def to_inline_html(msg: dict) -> str:
    """内联卡片片段（非完整 HTML 文档）。alert/confirm/report 摘要均适用。"""
    t = msg.get("type")
    accent = {"alert": "#ffc600", "confirm": "#e4002b", "report": "#1f9d55"}.get(t, "#ffc600")
    parts = [f'<div style="{_CARD_CSS}border-left:4px solid {accent};">']
    icon = {"alert": "🔔", "confirm": "⚠️", "report": "📊", "share": "📣"}.get(t, "🍔")
    parts.append(f'<div style="font-weight:700;margin-bottom:6px;">{icon} {msg.get("title", "")}</div>')
    if msg.get("price") is not None:  # confirm：数字高亮
        parts.append(
            '<div style="font-size:24px;font-weight:800;color:#e4002b;">'
            f'¥{float(msg["price"]):g}'
            f'<small style="font-size:12px;color:#5c6573;font-weight:400;">'
            f'（原价 ¥{float(msg.get("originalPrice") or 0):g}，省 ¥{float(msg.get("discount") or 0):g}）'
            "</small></div>"
        )
    for m in msg.get("metrics", []):
        color = "#e4002b" if m.get("highlight") else "#1d2129"
        parts.append(f'<span style="margin-right:14px;">{m["label"]}：<b style="color:{color};">{m["value"]}</b></span>')
    if msg.get("metrics"):
        parts.append("<br>")
    for ln in msg.get("lines", []):
        if ln:
            parts.append(f'<div style="color:#3a3f47;">· {ln}</div>')
    if t == "confirm":
        parts.append(f'<div style="margin-top:8px;color:#1d2129;">{msg.get("question", "")}</div>')
        for ch in msg.get("choices", []):
            primary = ch == "确认下单"
            style = ("background:#e4002b;color:#fff;" if primary else
                     "background:#f2f3f5;color:#1d2129;")
            parts.append(
                f'<button disabled style="{style}border:none;border-radius:8px;padding:6px 18px;'
                f'margin:8px 8px 0 0;font-size:14px;cursor:pointer;">{ch}</button>'
            )
        if msg.get("draftPath"):
            parts.append(f'<div style="font-size:11px;color:#8a7a4a;margin-top:6px;">'
                         f'CLI 确认：python main.py order --draft "{msg["draftPath"]}" --yes --confirm-text "确认下单"</div>')
    if msg.get("action"):
        parts.append(f'<div style="margin-top:8px;color:#1d2129;">👉 {msg["action"]}</div>')
    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 渲染层 3：HTML 整页（省钱账单 report / share 战报复用 share_card）
# ---------------------------------------------------------------------------
def to_report_html(msg: dict) -> str:
    """report → 完整 HTML 省钱账单页（细看/存档）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_saved = 0.0
    for s in msg.get("summary", []):
        try:
            total_saved += float(s.get("value", 0))
        except (TypeError, ValueError):
            pass
    parts = ["""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"/>
<style>
 body{margin:0;background:#f6f7f9;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1d2129;}
 .wrap{max-width:720px;margin:0 auto;padding:24px 20px 60px;}
 header{background:linear-gradient(135deg,#ffc600,#ffb000);border-radius:16px;padding:22px 26px;color:#3a2c00;margin-bottom:16px;}
 header h1{margin:0 0 4px;font-size:22px;}
 header .sub{font-size:12px;color:#6b5a20;}
 .summary{display:flex;gap:12px;margin:14px 0;flex-wrap:wrap;}
 .metric{background:#fff;border:1px solid #e6e8eb;border-radius:12px;padding:12px 18px;flex:1;min-width:140px;}
 .metric b{display:block;font-size:26px;color:#e4002b;}
 .metric span{font-size:12px;color:#5c6573;}
 section{background:#fff;border:1px solid #e6e8eb;border-radius:12px;padding:16px 20px;margin:12px 0;}
 h2{font-size:15px;margin:0 0 10px;padding-bottom:6px;border-bottom:2px solid #ffc600;display:inline-block;}
 table{width:100%;border-collapse:collapse;font-size:13px;}
 th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #f0f1f3;}
 th{color:#5c6573;font-weight:600;}
 .foot{text-align:center;font-size:11px;color:#b0a880;margin-top:16px;}
</style></head><body><div class="wrap">"""]
    parts.append(f"<header><h1>📊 {msg.get('title', '省钱账单')}</h1>"
                 f'<div class="sub">生成于 {now} · 麦麦省钱助理</div></header>')
    if msg.get("summary"):
        parts.append('<div class="summary">')
        for s in msg["summary"]:
            parts.append(f'<div class="metric"><b>{s.get("value", "")}</b><span>{s.get("label", "")}</span></div>')
        parts.append("</div>")
    for sec in msg.get("sections", []):
        parts.append(f"<section><h2>{sec.get('heading', '')}</h2>")
        if sec.get("lines"):
            parts.extend(f'<div style="font-size:13px;line-height:1.9;">· {ln}</div>' for ln in sec["lines"])
        tab = sec.get("table")
        if tab:
            parts.append("<table><tr>" + "".join(f"<th>{h}</th>" for h in tab.get("head", [])) + "</tr>")
            for row in tab.get("rows", []):
                parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
            parts.append("</table>")
        parts.append("</section>")
    parts.append('<div class="foot">麦麦省钱助理 · 你只管吃，省钱它来盯</div></div></body></html>')
    return "".join(parts)


def to_html(msg: dict) -> str:
    """统一入口：share→战报卡（含二维码）；report→账单页；其余→内联片段包整页。"""
    if msg.get("type") == "share":
        return ""  # share 的 HTML 在 share_card.render_card 已生成
    if msg.get("type") == "report":
        return to_report_html(msg)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"/>'
            f"<title>{msg.get('title', '')}</title></head>"
            f'<body style="margin:0;background:#f6f7f9;padding:12px;">'
            f"{to_inline_html(msg)}"
            f'<div style="text-align:center;font-size:11px;color:#b0a880;margin-top:10px;">'
            f"麦麦省钱助理 · {now}</div></body></html>")


def share_message_from_draft(draft: dict, drops: int = 0) -> tuple[dict, str]:
    """由 OrderDraft 产出 share 消息 + 战报卡 HTML（同数据源）。"""
    html = share_card.render_card(draft, drops=drops)
    msg = share(
        title="省钱战报已生成",
        image_path="",  # CLI 落盘后回填
        recipe_prompt=share_card.recipe_prompt(draft),
        saved=float(draft.get("discount") or 0),
    )
    msg["drops"] = drops
    return msg, html


def dumps(msg: dict) -> str:
    """结构化消息序列化（供宿主程序按 type 选择渲染方式）。"""
    return json.dumps(msg, ensure_ascii=False, indent=2, default=str)
