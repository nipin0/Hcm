"""Lot Calculator — five-mode lot size calculation for copy trading.

Computes follower lot size based on configurable calculation mode:

Modes:
  FIXED:        lot = fixed_lot
  MULTIPLIER:   lot = signal_lot × lot_multiplier, clamped to [min_lot, max_lot]
  RISK_PERCENT: lot = (balance × risk_percent / 100) / (sl_distance × pip_value)
  BALANCE_RATIO: lot = balance × balance_ratio
  EQUITY_RATIO:  lot = equity × equity_ratio

Features:
- Local in-memory cache for rules with hot-reload
- Per-account configuration from copy_configs
- Clamps output to [min_lot, max_lot]
- Handles missing data gracefully
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

DEFAULT_LOT_MODE = "MULTIPLIER"
DEFAULT_FIXED_LOT = 0.1
DEFAULT_LOT_MULTIPLIER = 1.0
DEFAULT_RISK_PERCENT = 1.0
DEFAULT_BALANCE_RATIO = 0.01
DEFAULT_EQUITY_RATIO = 0.01
DEFAULT_MIN_LOT = 0.01
DEFAULT_MAX_LOT = 5.0
DEFAULT_PIP_VALUE = 10.0  # USD per pip for standard lot


class LotMode(str, Enum):
    """Lot calculation modes."""
    FIXED = "FIXED"
    MULTIPLIER = "MULTIPLIER"
    RISK_PERCENT = "RISK_PERCENT"
    BALANCE_RATIO = "BALANCE_RATIO"
    EQUITY_RATIO = "EQUITY_RATIO"


@dataclass
class LotResult:
    """Result of a lot calculation."""
    lot: float = 0.0
    mode: str = "MULTIPLIER"
    original_lot: float = 0.0
    balance: float = 0.0
    equity: float = 0.0
    risk_percent: float = 0.0
    clamped: bool = False


class LotCalculator:
    """Calculates follower lot sizes for copy trading.

    Supports five lot modes: FIXED, MULTIPLIER, RISK_PERCENT,
    BALANCE_RATIO, and EQUITY_RATIO. Modes are configured per
    follower account in copy_configs.

    Uses local in-memory cache for configuration with hot-reload
    support from ConfigProviderV3.

    Example:
        calc = LotCalculator(db_pool=db_pool)
        lot = await calc.calculate(
            account_id=6, symbol="XAUUSD",
            signal_data={"lot": 0.1, "sl_price": 4100, "entry_price": 4110},
            copy_config={"lot_mode": "MULTIPLIER", "lot_multiplier": 2.0},
        )
    """

    def __init__(
        self,
        db_pool: Any = None,
        config_provider: Any = None,
    ):
        """Initialize LotCalculator.

        Args:
            db_pool: DatabasePool for querying account balances.
            config_provider: ConfigProviderV3 for rule parameters.
        """
        self._db = db_pool
        self._config = config_provider

        # Cached settings (hot-reloadable)
        self._default_pip_values: dict[str, float] = {
            "XAUUSD": 10.0,
            "XAGUSD": 50.0,
            "BTCUSD": 1.0,
            "ETHUSD": 0.1,
        }

    # ── Main API ────────────────────────────────

    async def calculate(
        self,
        account_id: int,
        symbol: str,
        signal_data: dict,
        copy_config: dict,
    ) -> float:
        """Calculate follower lot size for a copy trade.

        Args:
            account_id: Follower MT5 account ID.
            symbol: Follower trading symbol.
            signal_data: Original signal data dict.
            copy_config: Copy configuration dict from copy_configs table.

        Returns:
            Calculated lot size (clamped to [min_lot, max_lot]).
        """
        lot_mode = copy_config.get("lot_mode", DEFAULT_LOT_MODE)
        signal_lot = float(signal_data.get("lot", 0.1))
        min_lot = float(copy_config.get("min_lot", DEFAULT_MIN_LOT))
        max_lot = float(copy_config.get("max_lot", DEFAULT_MAX_LOT))

        balance, equity = await self._get_account_balance(account_id)

        logger.debug(
            "Lot calculation: account=%d, mode=%s, signal_lot=%s, "
            "balance=%s, equity=%s",
            account_id, lot_mode, signal_lot, balance, equity,
        )

        # Compute raw lot based on mode
        raw_lot = 0.0

        if lot_mode == LotMode.FIXED.value:
            raw_lot = float(copy_config.get("fixed_lot", DEFAULT_FIXED_LOT))

        elif lot_mode == LotMode.MULTIPLIER.value:
            multiplier = float(copy_config.get("lot_multiplier", DEFAULT_LOT_MULTIPLIER))
            raw_lot = signal_lot * multiplier

        elif lot_mode == LotMode.RISK_PERCENT.value:
            risk_pct = float(copy_config.get("risk_percent", DEFAULT_RISK_PERCENT))
            sl_distance = self._compute_sl_distance(signal_data)
            pip_value = await self._get_pip_value(symbol)
            if sl_distance > 0 and pip_value > 0:
                raw_lot = (balance * risk_pct / 100.0) / (sl_distance * pip_value)
            else:
                raw_lot = signal_lot  # Fallback

        elif lot_mode == LotMode.BALANCE_RATIO.value:
            ratio = float(copy_config.get("balance_ratio", DEFAULT_BALANCE_RATIO))
            raw_lot = balance * ratio

        elif lot_mode == LotMode.EQUITY_RATIO.value:
            ratio = float(copy_config.get("equity_ratio", DEFAULT_EQUITY_RATIO))
            raw_lot = equity * ratio

        else:
            logger.warning("Unknown lot mode '%s' — using MULTIPLIER fallback", lot_mode)
            raw_lot = signal_lot

        # Clamp to [min_lot, max_lot]
        clamped = raw_lot != max(min_lot, min(max_lot, raw_lot))
        final_lot = round(min(max_lot, max(min_lot, raw_lot)), 2)

        if clamped:
            logger.info(
                "Lot clamped: account=%d, raw=%s, clamped=%s, range=[%s, %s]",
                account_id, round(raw_lot, 4), final_lot, min_lot, max_lot,
            )

        logger.debug(
            "Calculated lot: account=%d, mode=%s, lot=%s",
            account_id, lot_mode, final_lot,
        )
        return final_lot

    # ── Helper Methods ──────────────────────────

    async def _get_account_balance(self, account_id: int) -> tuple[float, float]:
        """Get current balance and equity for an account.

        Args:
            account_id: MT5 account ID.

        Returns:
            Tuple of (balance, equity). Defaults to (0, 0).
        """
        if self._db is None or not self._db.is_initialized:
            return 0.0, 0.0

        try:
            row = await self._db.fetchrow(
                "SELECT COALESCE(balance, 0) as balance, "
                "COALESCE(equity, 0) as equity "
                "FROM hcm_trade.account_snapshots "
                "WHERE account_id=$1 ORDER BY created_at DESC LIMIT 1",
                account_id,
            )
            if row:
                return float(row["balance"]), float(row["equity"])
        except Exception as exc:
            logger.warning(
                "Failed to query balance for account_id=%s: %s", account_id, exc,
            )

        return 0.0, 0.0

    def _compute_sl_distance(self, signal_data: dict) -> float:
        """Compute absolute stop-loss distance in pips.

        Args:
            signal_data: Signal data dict with entry_price and sl_price.

        Returns:
            SL distance in price units (entry - sl).
        """
        entry = float(signal_data.get("entry_price", 0))
        sl = float(signal_data.get("sl_price", 0))
        if entry <= 0 or sl <= 0:
            return 0.0
        return abs(entry - sl)

    async def _get_pip_value(self, symbol: str) -> float:
        """Get pip value for a symbol, from cache or config.

        Args:
            symbol: Trading symbol.

        Returns:
            Pip value in account currency per standard lot.
        """
        # Check cache first
        if symbol in self._default_pip_values:
            return self._default_pip_values[symbol]

        # Try config provider
        if self._config is not None:
            try:
                return await self._config.get_float(
                    f"pip_value_{symbol}", DEFAULT_PIP_VALUE
                )
            except Exception:
                pass

        # Try DB
        if self._db is not None and self._db.is_initialized:
            try:
                row = await self._db.fetchval(
                    "SELECT pip_value FROM hcm_meta.symbol_meta WHERE symbol=$1",
                    symbol,
                )
                if row:
                    return float(row)
            except Exception:
                pass

        return DEFAULT_PIP_VALUE

    # ── Config Loading ──────────────────────────

    async def load_config(self) -> None:
        """Load lot calculation parameters from ConfigProviderV3."""
        if self._config is None:
            logger.warning("No ConfigProviderV3 — using default pip values")
            return

        try:
            # Load custom pip values
            for symbol in list(self._default_pip_values.keys()):
                pip_val = await self._config.get_float(
                    f"pip_value_{symbol}",
                    self._default_pip_values[symbol],
                )
                self._default_pip_values[symbol] = pip_val

            logger.info(
                "LotCalculator config loaded: %d pip values cached",
                len(self._default_pip_values),
            )
        except Exception as exc:
            logger.warning("LotCalculator config load failed: %s", exc)

    # ── Health ──────────────────────────────────

    async def health_check(self) -> dict:
        """Check calculator health.

        Returns:
            Dict with status.
        """
        return {
            "status": "healthy",
            "pip_values_cached": len(self._default_pip_values),
        }
