# -*- coding: utf-8 -*-
"""麦当劳官方 MCP 客户端（零第三方依赖，纯标准库）

- 端点: https://mcp.mcd.cn （Streamable HTTP, JSON-RPC 2.0）
- 鉴权: Authorization: Bearer <token>
- 限流: 600 次/分钟/token，内置滑动窗口自动控频
- 读写白名单：花钱动作（create-order 等）必须显式传 confirm=True 且经人类确认
- 响应兼容：直接 JSON 或 SSE（"data:" 行）
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2024-11-05"
RATE_LIMIT = 600  # 次/分钟
TIMEOUT = 40

# ---------------------------------------------------------------------------
# 读写白名单（红线：MONEY_WRITE 必须人类确认）
# ---------------------------------------------------------------------------
READ_ONLY = {
    "query-meals", "query-meal-detail", "query-nearby-stores",
    "delivery-query-stores", "delivery-query-addresses",
    "query-my-coupons", "query-store-coupons", "available-coupons",
    "calculate-price", "order-list", "query-order",
    "campaign-calendar", "now-time-info", "query-my-account",
    "mall-points-products", "mall-product-detail",
    "mall-order-list", "mall-order-detail",
    "list-nutrition-foods", "query-lottery-info", "query-my-prizes",
    "query-party-list", "query-party-detail", "query-meal-assistance",
}
FREE_WRITE = {
    "auto-bind-coupons",           # 免费领券：可自动，留回执
    "delivery-create-address",     # 新增配送地址：需用户给地址才调
}
MONEY_WRITE = {
    "create-order", "mall-create-order", "cancel-order", "draw-lottery",
    "party-order-create",
}


class McpError(RuntimeError):
    pass


class NetworkError(McpError):
    """网络层错误：请求可能已到达服务端但响应丢失，**结果不确定**。

    对非幂等写操作（create-order / cancel-order / mall-create-order 等），
    调用方遇到本异常时**必须禁止自动重试**，先查询订单状态再决定后续动作，
    否则可能造成重复下单。
    """


class McdClient:
    def __init__(self, token: str, endpoint: str = ENDPOINT):
        self.token = token
        self.endpoint = endpoint
        self._call_ts: list[float] = []   # 滑动窗口限流
        self._id = 0
        self._initialized = False

    # ---------------- 限流 ----------------
    def _throttle(self):
        now = time.time()
        self._call_ts = [t for t in self._call_ts if now - t < 60]
        if len(self._call_ts) >= RATE_LIMIT - 5:  # 留 5 次余量
            wait = 60 - (now - self._call_ts[0]) + 0.2
            time.sleep(max(wait, 0.2))
        self._call_ts.append(time.time())

    # ---------------- 底层 RPC ----------------
    def _post(self, payload: dict, retry_network: bool = True) -> dict | None:
        """发送 POST 并解析响应。

        retry_network=False 时，网络层错误（URLError）**不重试**，直接抛
        NetworkError——用于非幂等写操作，避免「服务端已执行但响应丢失」时
        重复提交造成重复订单。429 限流仍会重试（限流发生在业务处理之前，安全）。
        """
        self._throttle()
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + self.token,
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return self._parse_response(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise McpError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
            except (urllib.error.URLError, TimeoutError, socket.timeout,
                    ConnectionError) as e:
                # 传输层错误统一转 NetworkError：请求可能已到达服务端但响应丢失，
                # 结果不确定。写操作（retry_network=False）不重试、直接抛，交由上层
                # 查订单状态；读操作可安全重试。
                reason = getattr(e, "reason", None) or str(e)
                if retry_network and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise NetworkError(f"网络错误（结果不确定）: {reason}")
        raise McpError("请求重试耗尽（429）")

    @staticmethod
    def _parse_response(text: str) -> dict | None:
        text = text.strip()
        if not text:
            return None
        if text.startswith("{"):
            return json.loads(text)
        # SSE：取最后一个 data: 行
        data_lines = [
            ln[len("data:"):].strip()
            for ln in text.splitlines()
            if ln.startswith("data:")
        ]
        if not data_lines:
            raise McpError("无法解析的响应: " + text[:200])
        return json.loads(data_lines[-1])

    def rpc(self, method: str, params: dict | None = None, notify: bool = False,
            retry_network: bool = True):
        if notify:
            # 通知无 id、无响应
            payload = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            self._post(payload, retry_network=retry_network)
            return None
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = self._post(payload, retry_network=retry_network)
        if resp is None:
            raise McpError(f"{method}: 空响应")
        if "error" in resp:
            raise McpError(f"{method}: {resp['error']}")
        return resp.get("result")

    # ---------------- 生命周期 ----------------
    def initialize(self):
        if self._initialized:
            return
        self.rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-savings-assistant", "version": "0.1.0"},
        })
        self.rpc("notifications/initialized", notify=True)
        self._initialized = True

    # ---------------- 工具调用 ----------------
    def tools_list(self) -> list[dict]:
        self.initialize()
        result = self.rpc("tools/list", {})
        return result.get("tools", []) if result else []

    def call(self, name: str, arguments: dict | None = None, confirm: bool = False) -> Any:
        """调用官方工具。

        confirm: 花钱动作（MONEY_WRITE 白名单）必须显式传 True，
                 且调用方必须已获得账号持有者本人的明确确认。
        """
        self.initialize()
        if name in MONEY_WRITE and not confirm:
            raise McpError(
                f"红线拦截：{name} 是花钱/花积分操作，必须由账号持有者本人确认后"
                f"以 confirm=True 调用。当前调用被拒绝。"
            )
        if name not in READ_ONLY and name not in FREE_WRITE and name not in MONEY_WRITE:
            raise McpError(f"{name} 不在白名单中，拒绝调用（请先 tools_list 核对）。")
        # 写操作（FREE_WRITE + MONEY_WRITE）非幂等：网络层错误禁止自动重试，
        # 避免重复领券/重复建地址/重复下单。读操作可安全重试。
        write = name in FREE_WRITE or name in MONEY_WRITE
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}},
                          retry_network=not write)
        if result is None:
            raise McpError(f"{name}: 空结果")
        if result.get("isError"):
            raise McpError(f"{name} 执行出错: {json.dumps(result, ensure_ascii=False)[:500]}")
        sc = result.get("structuredContent")
        if sc is None:
            content = result.get("content") or []
            for c in content:
                if c.get("type") == "text":
                    try:
                        sc = json.loads(c["text"])
                    except (json.JSONDecodeError, KeyError):
                        return c["text"]
                    break
        # 业务错误信封：{success:false, code, message}
        if isinstance(sc, dict) and sc.get("success") is False:
            raise McpError(f"{name} 业务错误[{sc.get('code')}]: {sc.get('message')}")
        return sc

    # ---------------- 便捷只读封装 ----------------
    def now_time(self):
        return self.call("now-time-info")

    def my_account(self):
        return self.call("query-my-account")

    def nearby_stores(self, address: str, **kw):
        args = {"beType": 1, "searchType": 1, "address": address}
        args.update(kw)
        return self.call("query-nearby-stores", args)

    def meals(self, store_code: str, order_type: int = 1, be_type: int = 1,
              be_code: str | None = None):
        args = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
        if be_code:
            args["beCode"] = be_code   # 外卖(beType=2)/得来速(beType=5) 必传
        return self.call("query-meals", args)

    def delivery_addresses(self):
        """外送配送地址列表（data.addresses[]）。"""
        return self.call("delivery-query-addresses")

    def delivery_stores(self, address_id: str, be_type: int = 2):
        """按配送地址查可配送门店（data 为 list，含 storeCode/beCode）。"""
        return self.call("delivery-query-stores",
                         {"addressId": address_id, "beType": be_type})

    def create_address(self, **fields):
        """新增配送地址（FREE_WRITE，需用户提供真实信息）。"""
        return self.call("delivery-create-address", fields)

    def my_coupons(self):
        return self.call("query-my-coupons")

    def store_coupons(self, store_code: str, order_type: int = 1, be_type: int = 1,
                      be_code: str | None = None):
        args = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
        if be_code:
            args["beCode"] = be_code   # 外卖/得来速必传
        return self.call("query-store-coupons", args)

    def available_coupons(self):
        return self.call("available-coupons")

    def order_list(self):
        return self.call("order-list")

    def query_order(self, order_id: str):
        return self.call("query-order", {"orderId": order_id})
