"""Notifier — 下单事件推送到钉钉(加签) / 企业微信。

设计原则(对齐项目"禁用硬编码"):
- 通道 URL / secret / 开关全部来自 PG hcm_config.metadata
  (config_key 形如 notification.dingtalk_webhook), 零硬编码。
- 挂在 OrderTracker 的 PLACED 状态跃迁上, 用 asyncio.create_task 非阻塞广播,
  绝不阻塞订单流转关键路径。
- 零第三方依赖: 用标准库 urllib + asyncio.to_thread 做异步 POST
  (dispatcher 依赖里没有 httpx/aiohttp, 避免为加依赖重建镜像)。
- 钉钉群机器人: 对 timestamp+secret 做 HMAC-SHA256 加签后追加到 webhook URL。
- 企业微信: 群机器人 webhook 直接 POST markdown (个人微信经"微信插件"转发)。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 通知相关的 config_key 全集 (与 hcm-web/web/api/system.py 的
# NOTIFICATION_CONFIG_KEYS 保持一致, 这里是 notifier 实际读取的子集)
CONFIG_KEYS = [
    "notification.dingtalk_webhook",
    "notification.dingtalk_secret",
    "notification.wecom_webhook",
    "notification.wecom_secret",
    "notification.enable_trade_alert",
    "notification.enable_signal_alert",
    "notification.enable_risk_alert",
    "notification.enable_system_alert",
    "notification.timezone_offset",
]

CONFIG_DEFAULTS = {
    "notification.dingtalk_webhook": "",
    "notification.dingtalk_secret": "",
    "notification.wecom_webhook": "",
    "notification.wecom_secret": "",
    "notification.enable_trade_alert": "true",
    "notification.enable_signal_alert": "true",
    "notification.enable_risk_alert": "true",
    "notification.enable_system_alert": "false",
    "notification.timezone_offset": "8.0",
}

# 配置缓存 TTL: 避免每次下单都查 PG (下单频率低, 60s 足够)
CONFIG_CACHE_TTL = 60.0
HTTP_TIMEOUT = 3.0
HTTP_RETRIES = 2


def _is_true(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes", "on")


class Notifier:
    """下单通知广播器。

    用法 (在 hcm-dispatcher/main.py 装配):
        notifier = Notifier(db_pool=db_pool)
        order_tracker = OrderTracker(order_timeout=..., notifier=notifier)
    订单进入 PLACED 时, OrderTracker 调用:
        asyncio.create_task(notifier.on_order(order))
    """

    def __init__(self, db_pool: Any = None, config_cache_ttl: float = CONFIG_CACHE_TTL):
        self._db = db_pool
        self._cache: dict = {}
        self._cache_at: float = 0.0
        self._cache_ttl = config_cache_ttl
        self._lock = asyncio.Lock()

    # ── 配置加载 (PG hcm_config.metadata) ─────────

    async def _load_config(self) -> dict:
        now = time.time()
        if self._cache and (now - self._cache_at) < self._cache_ttl:
            return self._cache
        async with self._lock:
            # double-check after acquiring lock
            if self._cache and (time.time() - self._cache_at) < self._cache_ttl:
                return self._cache
            cfg = dict(CONFIG_DEFAULTS)
            if self._db is not None and getattr(self._db, "is_initialized", False):
                try:
                    rows = await self._db.fetch(
                        "SELECT config_key, current_value, default_value "
                        "FROM hcm_config.metadata WHERE config_key = ANY($1::text[])",
                        CONFIG_KEYS,
                    )
                    for r in rows:
                        cv = r.get("current_value")
                        dv = r.get("default_value")
                        val = cv if (cv is not None and str(cv).strip() != "") else dv
                        if r["config_key"] in CONFIG_KEYS:
                            cfg[r["config_key"]] = val
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Notifier config load failed: %s", exc)
                    # 保留旧缓存(若有), 避免配置短暂不可用时完全失声
                    if self._cache:
                        return self._cache
            self._cache = cfg
            self._cache_at = time.time()
            return cfg

    # ── 下单事件入口 (非阻塞) ─────────────────────

    async def on_order(self, order: Any) -> None:
        """下单事件入口。由 OrderTracker 通过 create_task 调用, 自身不阻塞调用方。"""
        try:
            cfg = await self._load_config()
            if not _is_true(cfg.get("notification.enable_trade_alert", "false")):
                return

            # 时区偏移(小时, 东为正, 默认东八区 Asia/Shanghai), 把容器 UTC 时间转本地时间
            try:
                tz_offset = float(cfg.get("notification.timezone_offset", "8.0"))
            except (ValueError, TypeError):
                tz_offset = 8.0

            md = self._build_markdown(order, tz_offset)

            # 通道启用判断（勾选使用）：仅当「已勾选启用」且「已配置 Webhook」才推送；
            # 未勾选 → 完全跳过、不发请求；勾选但未配置 Webhook → 静默跳过（仅 info，不报错）。
            dt_enabled = _is_true(cfg.get("notification.dingtalk_enabled", True))
            dt_url = (cfg.get("notification.dingtalk_webhook") or "").strip()
            dt_secret = (cfg.get("notification.dingtalk_secret") or "").strip()
            wx_enabled = _is_true(cfg.get("notification.wecom_enabled", True))
            wx_url = (cfg.get("notification.wecom_webhook") or "").strip()

            if dt_enabled and dt_url:
                signed = self._sign_dingtalk(dt_url, dt_secret)
                await self._post_json(
                    signed,
                    {"msgtype": "markdown", "markdown": {"title": "HCM 新订单", "text": md}},
                )
            elif dt_enabled and not dt_url:
                logger.info("DingTalk enabled but webhook empty — skipped (no error)")

            if wx_enabled and wx_url:
                await self._post_json(
                    wx_url,
                    {"msgtype": "markdown", "markdown": {"content": md}},
                )
            elif wx_enabled and not wx_url:
                logger.info("WeCom enabled but webhook empty — skipped (no error)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Notifier.on_order failed: %s", exc)

    # ── 消息内容 ──────────────────────────────────

    @staticmethod
    def _extract(order: Any) -> dict:
        """从 TrackedOrder 或 dict 中抽取需要的字段。"""
        def get(field, default=""):
            if isinstance(order, dict):
                return order.get(field, default)
            return getattr(order, field, default)
        return {
            "account_id": get("account_id", "?"),
            "account_role": get("account_role", ""),
            "symbol": get("symbol", "?"),
            "direction": str(get("direction", "") or "").upper(),
            "lot": get("lot", 0.0),
            "filled_price": get("filled_price", 0.0),
            "entry_price": get("entry_price", 0.0),
            "mt5_ticket": get("mt5_ticket", 0),
            "ts": get("ts", 0),
        }

    @staticmethod
    def _format_local_time(ts: Any, tz_offset: float) -> str:
        """把 Unix 时间戳(秒, UTC) 转换为 UTC+tz_offset 的本地时间字符串。

        tz_offset 为东向偏移小时数(如 8.0 = 东八区 Asia/Shanghai)。
        时间戳缺失或为 0 时回退到当前 UTC 时间再换算。
        零外部依赖: 用标准库 datetime + 固定偏移 timezone 实现, 无需 tzdata。
        """
        try:
            ts_f = float(ts) if ts not in (None, "", 0, 0.0) else 0.0
        except (ValueError, TypeError):
            ts_f = 0.0
        if ts_f <= 0:
            ts_f = time.time()
        dt_utc = datetime.fromtimestamp(ts_f, tz=timezone.utc)
        local = dt_utc.astimezone(timezone(timedelta(hours=tz_offset)))
        sign = "+" if tz_offset >= 0 else "-"
        label = f"UTC{sign}{abs(tz_offset):g}"
        return f"{local.strftime('%Y-%m-%d %H:%M:%S')} ({label})"

    def _build_markdown(self, order: Any, tz_offset: float = 8.0) -> str:
        """构造 markdown 文本。核心三要素: 有新订单 / 进单价格 / 手数 + 本地时间。"""
        d = self._extract(order)

        # 进单价格: 优先实际成交价, 回退信号入场价
        price = d["filled_price"] or d["entry_price"]
        try:
            price_s = f"{float(price):.2f}" if price else "—"
        except (ValueError, TypeError):
            price_s = "—"

        direction = d["direction"]
        # 红涨绿跌(中国习惯): BUY 红 🔴, SELL 绿 🟢
        if direction == "BUY":
            dir_disp = "🔴 买入 BUY"
        elif direction == "SELL":
            dir_disp = "🟢 卖出 SELL"
        else:
            dir_disp = direction or "—"

        role_label = {"master": "主号", "follower": "跟单号", "standalone": "独立"}.get(
            (d.get("account_role") or ""), ""
        )
        time_s = self._format_local_time(d.get("ts", 0), tz_offset)
        return (
            "### 🔔 有新订单啦\n"
            f"> **账户**: {d['account_id']}" + (f" ({role_label})" if role_label else "") + "\n"
            f"> **品种**: {d['symbol']}\n"
            f"> **方向**: {dir_disp}\n"
            f"> **进单价格**: {price_s}\n"
            f"> **手数**: {d['lot']}\n"
            f"> **时间**: {time_s}\n"
            f"> ticket: {d['mt5_ticket']}\n"
        )

    # ── 钉钉加签 ──────────────────────────────────

    @staticmethod
    def _sign_dingtalk(url: str, secret: str) -> str:
        """钉钉群机器人加签: timestamp+secret 做 HMAC-SHA256 → base64 → urlencode。"""
        if not secret:
            return url
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        sign = base64.b64encode(
            hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"

    # ── HTTP POST (非阻塞) ────────────────────────

    @staticmethod
    async def _post_json(url: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> bool:
        data = json.dumps(payload).encode("utf-8")

        def _sync() -> tuple[int, str]:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "ignore")

        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                status, body = await asyncio.to_thread(_sync)
                if 200 <= status < 300:
                    logger.info("Notifier push ok → %s", url[:64])
                    return True
                logger.warning("Notifier push HTTP %s: %s", status, body[:200])
            except urllib.error.HTTPError as exc:
                logger.warning("Notifier push HTTPError %s: %s", exc.code, exc.reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Notifier push attempt %d/%d failed: %s", attempt, HTTP_RETRIES, exc)
            if attempt < HTTP_RETRIES:
                await asyncio.sleep(0.5)
        return False
