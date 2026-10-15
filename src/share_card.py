# -*- coding: utf-8 -*-
"""H/I · 下单前优惠方案与 order recipe（无凭据，可复现）

v0.15（方案 14.3）后职责收窄：
  - 渲染层（内联卡片 / HTML 页面 / 图片）统一由 present.py 按 payload.type 负责；
  - 本模块只保留与渲染无关的逻辑：配方构造、紧凑化、二维码、复现提示词、旧版卡片（兼容）。

红线：分享图只承载"订单配方 + 提示信息"，不承载执行权限；
好友侧任何 create-order 前必须由其本人确认。
"""
import json
from html import escape
from datetime import datetime

from .qr import qr_svg, PayloadTooLarge

# 紧凑配方：控制在 ~100 字节内以适配 MVP 二维码（v5）
RECIPE_PROMPT = (
    "请用「麦麦省钱助理」(https://github.com/YOUR_REPO) 帮我复现这一单："
    "门店 {storeCode}，商品 {items}，就餐方式 takeWayCode={takeWayCode}，"
    "券 {coupon}。先算价给我看，我确认后再下单。"
)


def build_recipe(draft: dict) -> dict:
    """从 OrderDraft 提炼可分享配方（绝不含 token）。"""
    return {
        "v": 1,
        "s": draft.get("storeCode"),
        "t": draft.get("takeWayCode") or "",
        "i": [[it["productCode"], it["quantity"]] for it in draft.get("items", [])],
        "c": (draft.get("coupon") or {}).get("couponCode", ""),
    }


def recipe_json(draft: dict) -> str:
    return json.dumps(build_recipe(draft), ensure_ascii=False, separators=(",", ":"))


def recipe_prompt(draft: dict) -> str:
    r = build_recipe(draft)
    items = ";".join(f"{c} x{n}" for c, n in r["i"])
    return RECIPE_PROMPT.format(
        storeCode=r["s"], items=items, takeWayCode=r["t"] or "见 calculate-price",
        coupon=r["c"] or "无",
    )


def render_card(draft: dict, drops: int = 0) -> str:
    """渲染 HTML 优惠方案卡；动态字段转义后再嵌入 HTML。"""
    saved = float(draft.get("discount") or 0)
    coupon = draft.get("coupon")
    store_name = escape(str(draft.get("storeName") or draft.get("storeCode") or ""))
    coupon_line = (
        f"报价用券「{escape(str(coupon.get('name') or coupon.get('couponCode')))}」；"
        f"预计优惠 -¥{saved:g}"
        if coupon else "本单未用券/积分"
    )
    items = "<br>".join(
        f"{escape(str(it.get('name') or it.get('productCode')))} × {escape(str(it.get('quantity')))}"
        for it in draft.get("items", [])
    )
    recipe = escape(recipe_json(draft))
    qr_html = ""
    try:
        qr = qr_svg(recipe_json(draft), module=5)
        qr_html = f'<div class="qr">{qr}<p>好友扫码导入这单（须本人确认下单）</p></div>'
    except PayloadTooLarge as e:
        qr_html = f'<div class="qr none"><p>（订单过大，未生成二维码）<br>{escape(str(e))}</p></div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"/>
<style>
 body{{margin:0;background:#f6f7f9;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}}
 .card{{max-width:420px;margin:24px auto;background:linear-gradient(160deg,#ffc600,#ffb000);
   border-radius:18px;padding:2px;box-shadow:0 6px 24px rgba(0,0,0,.12);}}
 .inner{{background:#fffdf5;border-radius:16px;padding:22px 24px;}}
 h1{{margin:0 0 4px;font-size:20px;color:#3a2c00;}}
 .sub{{color:#8a7a4a;font-size:12px;margin-bottom:14px;}}
 .save{{font-size:34px;font-weight:800;color:#e4002b;line-height:1.2;}}
 .save small{{font-size:14px;color:#5c6573;font-weight:400;}}
 .rows{{margin:14px 0;font-size:14px;color:#1d2129;line-height:1.9;}}
 .rows b{{float:right;}}
 .coupon{{display:inline-block;background:#fdeaea;color:#dc2626;border-radius:12px;
   padding:2px 10px;font-size:12px;margin-top:6px;}}
 .recipe{{background:#f2f3f5;border-radius:8px;padding:10px 12px;font-size:11px;
   word-break:break-all;color:#5c6573;margin-top:12px;}}
 .qr{{text-align:center;margin-top:14px;}}
 .qr p,.qr.none p{{font-size:12px;color:#5c6573;margin:6px 0 0;}}
 .foot{{text-align:center;font-size:11px;color:#b0a880;margin-top:14px;}}
</style></head><body>
<div class="card"><div class="inner">
 <h1>麦麦优惠方案</h1>
 <div class="sub">{datetime.now().strftime('%Y-%m-%d %H:%M')} · 门店 {store_name}</div>
 <div class="save">预计优惠 ¥{saved:g}<br><small>尚未下单；此金额未计入实际省下</small><br><small>报价到手价 ¥{draft.get('price', 0):g}（原价 ¥{draft.get('originalPrice', 0):g}）</small></div>
 <span class="coupon">{coupon_line}</span>
 {f'<div class="coupon" style="background:#e6f6ed;color:#1f9d55">本月抓住 {drops} 次降价</div>' if drops else ''}
 <div class="rows">{items}</div>
 <div class="recipe"><b>order recipe</b>（不含任何凭据）<br>{recipe}</div>
 {qr_html}
 <div class="foot">麦麦省钱助理 · 你只管吃，省钱它来盯</div>
</div></div></body></html>"""
