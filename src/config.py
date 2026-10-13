# -*- coding: utf-8 -*-
"""麦麦省钱助理 · 配置加载

优先级：环境变量 MCD_MCP_TOKEN > config.local.json > 报错提示。
私密信息（token/地址/称呼）只存 config.local.json（已被 .gitignore 排除）。
"""
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = PROJECT_ROOT / "config.local.json"
EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.json"

# 缓存目录：数据只存本地
CACHE_DIR = Path(os.environ.get("MCD_SKILL_DATA", str(Path.home() / ".mcd-skill")))


def _load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        print(f"[config] {path.name} JSON 解析失败: {e}", file=sys.stderr)
        return None


class Config:
    def __init__(self):
        raw = _load_json(LOCAL_CONFIG) or {}

        # --- token：环境变量优先 ---
        self.token = os.environ.get("MCD_MCP_TOKEN") or raw.get("mcp_token") or ""
        if not self.token or self.token == "YOUR_MCP_TOKEN":
            print(
                "未找到麦当劳 MCP Token！\n"
                "  方式1：复制 config.example.json 为 config.local.json，填入 mcp_token\n"
                "  方式2：设置环境变量 MCD_MCP_TOKEN\n"
                "  领取地址：https://open.mcd.cn/mcp （手机号登录 → 控制台激活，免费）",
                file=sys.stderr,
            )
            sys.exit(2)

        user = raw.get("user", {}) or {}
        self.nickname = user.get("nickname") or "老大"
        self.default_address = user.get("default_address") or ""
        self.favorite_stores = user.get("favorite_stores") or []
        self.delivery_address = user.get("delivery_address") or ""

        agent = raw.get("agent", {}) or {}
        self.scan_store_limit = int(agent.get("scan_store_limit") or 3)
        self.new_low_ratio = float(agent.get("new_low_ratio") or 1.0)
        self.expire_points_threshold = int(agent.get("expire_points_threshold") or 100)

    def personal_gaps(self):
        """列出待用户补充的个人信息（用于首启提醒）。"""
        gaps = []
        if not self.default_address:
            gaps.append("user.default_address：常用地址（查附近门店用）")
        if not self.favorite_stores:
            gaps.append("user.favorite_stores：常去门店 storeCode 列表")
        if not self.delivery_address:
            gaps.append("user.delivery_address：外卖配送地址（可选）")
        return gaps

    def save_user_fields(self, **fields) -> list[str]:
        """回写 config.local.json 的 user 段（仅本地文件，已被 gitignore）。

        只允许白名单键，防止误写 token 等敏感字段。
        返回实际写入的键列表。
        """
        allowed = {"nickname", "default_address", "favorite_stores", "delivery_address"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"不允许写入的字段: {bad}")
        raw = _load_json(LOCAL_CONFIG) or {}
        user = raw.setdefault("user", {})
        written = []
        for k, v in fields.items():
            if v in (None, "", []):
                continue
            if user.get(k) != v:
                user[k] = v
                written.append(k)
        if written:
            LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            with open(LOCAL_CONFIG, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
            # 刷新内存缓存
            self.default_address = user.get("default_address") or ""
            self.delivery_address = user.get("delivery_address") or ""
            self.favorite_stores = user.get("favorite_stores") or []
            self.nickname = user.get("nickname") or "老大"
        return written


_cache: Config | None = None


def get_config() -> Config:
    global _cache
    if _cache is None:
        _cache = Config()
    return _cache
