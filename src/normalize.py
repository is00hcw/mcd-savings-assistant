# -*- coding: utf-8 -*-
"""商品名称归一化

实测结论（产品方案 ⑫）：同商品跨店 code 不同（如「吉士蛋麦满分组合」
A店 9900008094 / B店 9900003432），因此低价记忆、购买统计、商品偏好
一律以 name（归一化后）为主键；code 仅作门店内下单凭证。
"""
import re
import unicodedata

# 别名表兜底：别名 -> 标准名（可持续补充）
ALIAS_TABLE = {
    "中份薯条": "中薯条",
    "大份薯条": "大薯条",
    "小份薯条": "小薯条",
    "薯条(中)": "中薯条",
    "薯条(大)": "大薯条",
    "薯条(小)": "小薯条",
    "可口可乐(中)": "中可乐",
    "中杯可乐": "中可乐",
}

_FULL2HALF = {
    "（": "(", "）": ")", "：": ":", "，": ",", "。": ".",
    "！": "!", "？": "?", "＆": "&", "～": "~", "－": "-",
}


def normalize_name(name: str) -> str:
    """商品名称归一化：
    ① strip 首尾空格 ② 全角→半角（含 NFKC）③ 压缩空白 ④ 别名表兜底
    """
    if not name:
        return ""
    s = name.strip()
    # NFKC 兼容分解会把全角字母数字转半角，但对全角标点不全，先手动映射
    for k, v in _FULL2HALF.items():
        s = s.replace(k, v)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lower()
    return ALIAS_TABLE.get(s, s)


def meal_index_from_query_meals(data: dict) -> dict[str, dict]:
    """从 query-meals 响应提取 {归一化name: meal}。

    实测：价格在 data.meals（dict, key=code），categories 里无价格。
    返回 value 含 code/name/currentPrice/originalPrice/discountType。
    """
    out: dict[str, dict] = {}
    meals = (data or {}).get("meals") or {}
    if isinstance(meals, dict):
        # 实测：meals 为 dict，key=code，value 内可能不含 code 字段 → 注入
        items = []
        for code_key, m in meals.items():
            if isinstance(m, dict):
                m = dict(m)
                if not m.get("code"):
                    m["code"] = code_key
                items.append(m)
    elif isinstance(meals, list):
        items = meals
    else:
        return out
    for m in items:
        name = m.get("name") or ""
        key = normalize_name(name)
        if not key:
            continue
        # 同名取价更低者（跨档位罕见，防御性处理）
        cur = out.get(key)
        if cur is None or _price_of(m) < _price_of(cur):
            out[key] = m
    return out


def _price_of(m: dict) -> float:
    try:
        return float(m.get("currentPrice") or 0)
    except (TypeError, ValueError):
        return float("inf")
