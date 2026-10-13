# -*- coding: utf-8 -*-
"""首次使用引导 + Agent 自述元信息

三件事：
  welcome()      新用户/新会话看到的第一屏（中文人话，不用 argparse usage 报错糊脸）
  doctor()       自检：环境→配置→token 有效性→地址/门店就绪→下一步（全程只读）
  capabilities() 给 Agent 读的元信息：我是谁/我会什么/我绝不做什么
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import config as _config          # 用模块属性访问，避免路径被缓存成值拷贝
from .config import get_config, PROJECT_ROOT, CACHE_DIR

TOKEN_URL = "https://open.mcd.cn/mcp"
WORKBUDDY_DOC = "https://www.workbuddy.cn/docs/workbuddy/Overview"

FIRST_RUN_STEPS = [
    ("① 领 Token", f"手机号登录 {TOKEN_URL} → 右上角【控制台】→ 点【激活】→ 复制 MCP Token",
     "免费，约 1 分钟。没 Token 后面所有功能都用不了"),
    ("② 填配置", "复制 config.example.json 为 config.local.json，把 Token 粘进 mcp_token",
     "config.local.json 已被 .gitignore 排除，不会泄露到仓库"),
    ("③ 跑热身", "python main.py warmup && python main.py patrol",
     "warmup 导入历史订单建低价档案，patrol 开始盯价"),
]


def welcome() -> str:
    """无参数运行时的第一屏。"""
    nick = "老大"
    try:
        nick = get_config().nickname
    except SystemExit:
        pass
    lines = [
        f"🍔 麦麦省钱助理 · 你的省钱盯价闹钟（{nick}）",
        "",
        f"首次使用三步（2 分钟内可跑通）：",
    ]
    for i, (title, how, why) in enumerate(FIRST_RUN_STEPS, 1):
        lines.append(f"  {title}：{how}")
        lines.append(f"     └ {why}")
    lines += [
        "",
        "不确定配好了没？先跑这个：python main.py doctor",
        "不确定我能干什么？跑这个：python main.py capabilities",
        "",
        "常用命令：",
        "  warmup    首启热身（导入订单建低价档案）",
        "  patrol    盯价巡检（降价 / 可领券 / 临期积分）",
        "  best      算最低到手价并生成确认卡（不自动下单）",
        "  report    省钱账单（HTML 页面）   share  省钱战报（图片）",
        "  coupons   可领券预览（默认只读，不动账户）",
        "",
        "⛔ 下单/积分兑换/抽奖 属花钱动作，必须你本人确认才会执行。",
        "📖 完整说明见 README.md；在 WorkBuddy 中对话使用见 skill.md。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def doctor(check_token: bool = True) -> dict:
    """自检清单（只读）。返回结构化结果 + 人类可读文本。"""
    checks: list[dict] = []

    def add(name: str, ok: bool | None, detail: str, fix: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    # ① 环境
    ver_ok = sys.version_info >= (3, 9)
    add("Python 版本", ver_ok, f"{platform.python_version()}（要求 ≥3.9）",
        "升级 Python" if not ver_ok else "")
    add("第三方依赖", True, "零依赖，仅标准库")

    # ② 配置文件
    if not _config.LOCAL_CONFIG.exists():
        add("config.local.json", False, "文件不存在",
            "复制模板：cp config.example.json config.local.json")
        return {"checks": checks, "ready": False,
                "text": _render(checks, next_steps=["先完成上面第 ② 步：复制配置模板并填入 Token"])}
    add("config.local.json", True, str(_config.LOCAL_CONFIG))

    # ③ Token
    try:
        cfg = get_config()
        token_ok = bool(cfg.token) and cfg.token != "YOUR_MCP_TOKEN"
        add("MCP Token", token_ok, "已配置（仅本地保存）" if token_ok else "未配置",
            f"去 {TOKEN_URL} 手机号登录 → 控制台 → 激活 → 复制 Token" if not token_ok else "")
    except SystemExit:
        add("MCP Token", False, "未配置或仍是占位符",
            f"去 {TOKEN_URL} 手机号登录 → 控制台 → 激活")
        return {"checks": checks, "ready": False,
                "text": _render(checks, next_steps=["填好 Token 后重跑 doctor"])}

    # ④ Token 有效性（只读探测）
    if check_token:
        from .mcd_client import McdClient, McpError
        try:
            r = McdClient(cfg.token).now_time()
            dt = ((r or {}).get("data") or {}).get("formatted") or "已连通"
            add("Token 连通性", True, f"官方服务器返回时间 {dt}")
        except McpError as e:
            add("Token 连通性", False, f"鉴权或网络异常：{str(e)[:80]}",
                "确认 Token 未过期；检查网络或代理")
        except Exception as e:
            add("Token 连通性", False, f"{type(e).__name__}: {str(e)[:80]}")

    # ⑤ 地址 / 门店就绪
    add("常用地址", bool(cfg.default_address),
        cfg.default_address or "未配置（可自动推导）",
        "python main.py address --apply（从麦乐送已存地址推导）" if not cfg.default_address else "")
    add("常去门店", bool(cfg.favorite_stores),
        f"{len(cfg.favorite_stores)} 家：{cfg.favorite_stores}" if cfg.favorite_stores
        else "未配置（巡检会跳过）",
        "python main.py warmup --apply-stores（从订单推导）" if not cfg.favorite_stores else "")

    # ⑥ 本地数据目录
    add("本地数据目录", True, str(CACHE_DIR) + ("（已有数据）" if CACHE_DIR.exists() else "（尚未生成）"))

    ready = all(c["ok"] for c in checks if c["ok"] is not None)
    if ready:
        steps = [
            "一切就绪。建议按这个顺序体验：",
            "  1) python main.py warmup    # 导入历史订单，建低价档案与常买 Top3",
            "  2) python main.py patrol    # 巡检常去门店，抓降价 / 可领券 / 临期积分",
            "  3) python main.py best --store <门店code> --items \"经典麦旋风 x1\"   # 算最低到手价（不会下单）",
            "  4) python main.py report    # 看省钱账单   python main.py share --draft <草稿>  # 战报图",
        ]
    else:
        steps = [f"· {c['fix']}" for c in checks if c["ok"] is False and c["fix"]] or [
            "配置看起来没问题，跑 python main.py warmup 开始热身"]
    return {"checks": checks, "ready": ready, "text": _render(checks, steps)}


def _render(checks: list[dict], next_steps: list[str]) -> str:
    lines = ["【自检结果】"]
    for c in checks:
        mark = "✅" if c["ok"] else ("⚠️ " if c["ok"] is None else "❌")
        lines.append(f"{mark} {c['name']}：{c['detail']}")
        if c["ok"] is False and c["fix"]:
            lines.append(f"    → 修复：{c['fix']}")
    lines.append("")
    lines.append("下一步：")
    lines.extend(next_steps)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def capabilities() -> dict:
    """Agent 自述元信息：能力、命令、红线、输出类型。"""
    return {
        "name": "麦麦省钱助理",
        "englishName": "mcd-savings-assistant",
        "tagline": "你的省钱盯价闹钟——到点叫你捡便宜",
        "positioning": "副驾（默认不扣款），而非全自动下单工具",
        "basedOn": {"mcpServer": "https://mcp.mcd.cn",
                    "protocol": "Streamable HTTP + JSON-RPC 2.0",
                    "auth": "Bearer Token（本地保存，不入库）"},
        "entry": {"cli": "main.py", "skill": "skill.md", "python": "≥3.9，零第三方依赖"},
        "capabilities": [
            {"code": "A", "name": "低价记忆", "desc": "query-meals 建历史最低价基线；订单冷启动；商品名归一化后跨店比价",
             "cli": "warmup / meals --save"},
            {"code": "B", "name": "主动感知", "desc": "多店巡检，检出跨期降价、历史新低、可领券、临期积分",
             "cli": "patrol"},
            {"code": "C", "name": "自动领券", "desc": "可领券预览（默认只读）；--claim --yes 才领取并留回执",
             "cli": "coupons"},
            {"code": "D", "name": "最优下单", "desc": "带券线性枚举算最低到手价，输出「全局最低/常买Top3」双轨候选与确认卡",
             "cli": "best"},
            {"code": "G", "name": "定时与确认", "desc": "草稿确认闸三层拦截，未经用户确认绝不执行花钱动作",
             "cli": "order"},
            {"code": "H", "name": "省钱账单", "desc": "账本聚合 drop 估算 + order 真实优惠，产出可存档 HTML",
             "cli": "report"},
            {"code": "I", "name": "省钱战报", "desc": "分享用图片 + 不含凭据的 order recipe（二维码复现）",
             "cli": "share"},
            {"code": "—", "name": "地址自动推导", "desc": "从麦乐送已存地址推导常用地址，订单门店地址兜底",
             "cli": "address / warmup --apply-address"},
        ],
        "outputContract": {
            "types": ["alert", "confirm", "report", "share"],
            "renderers": ["inline（内联卡片/Markdown 兜底）", "html（页面）", "svg（图片）", "json（结构化）"],
            "rule": "核心逻辑只产出 payload，渲染层可插拔；图片只分享不交互，确认闸绝不靠图片",
        },
        "hardRules": [
            "create-order / mall-create-order / cancel-order / draw-lottery 必须用户本人明确确认",
            "领券（auto-bind-coupons）需显式 --claim --yes",
            "MCP Token 与个人信息只存 config.local.json（已 gitignore），不写入任何输出与仓库",
            "限流 600 次/分钟，客户端内置滑动窗口控频",
            "分享图只给配方不给执行权限",
        ],
        "onboarding": {
            "firstScreen": "python main.py（打印欢迎页与三步引导）",
            "selfCheck": "python main.py doctor",
            "steps": [{"title": t, "how": h, "why": w} for t, h, w in FIRST_RUN_STEPS],
        },
        "voice": "称呼用户「老大」；先报结果再报省了多少；到点有画面感；确认话术固定",
    }


def capabilities_text() -> str:
    cap = capabilities()
    lines = [f"🍔 {cap['name']}（{cap['englishName']}）——{cap['tagline']}",
             f"定位：{cap['positioning']}",
             f"入口：{cap['entry']['cli']} ｜ 运行环境：{cap['entry']['python']}",
             "",
             "我能做的："]
    for c in cap["capabilities"]:
        lines.append(f"  [{c['code']}] {c['name']}：{c['desc']}")
        lines.append(f"      命令：{c['cli']}")
    lines += ["", "输出契约：type = " + " / ".join(cap["outputContract"]["types"]),
              "  渲染：" + "、".join(cap["outputContract"]["renderers"]),
              "  " + cap["outputContract"]["rule"],
              "", "⛔ 绝不自动做的事："]
    lines += [f"  · {r}" for r in cap["hardRules"]]
    lines += ["", f"首次使用：python main.py doctor（自检）→ {cap['onboarding']['steps'][0]['how']}"]
    return "\n".join(lines)