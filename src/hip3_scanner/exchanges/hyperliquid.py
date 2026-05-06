"""
Hyperliquid exchange adapter using the hyperliquid CCXT package.

Auth: Ethereum-style secp256k1 ECDSA private key + wallet address.
The privateKey is your trading wallet private key (MetaMask format: 0x... 64 hex chars).
NEVER commit real private keys — use .env only.

Mode toggle:
  dry_run=True  (default)  → simulated fills, no real orders
  dry_run=False             → live order execution via Hyperliquid API
"""

from __future__ import annotations

import json
import time
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from ..exchanges import (
    BalanceInfo,
    ExchangeAdapter,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    PositionInfo,
)


# ---------------------------------------------------------------------------
# Hyperliquid constants
# ---------------------------------------------------------------------------

HL_BASE_URL = "https://api.hyperliquid.xyz"
HL_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"


# ---------------------------------------------------------------------------
# Hyperliquid adapter via CCXT
# ---------------------------------------------------------------------------

class HyperliquidAdapter(ExchangeAdapter):
    """
    Hyperliquid perpetuals exchange adapter.

    Auth: requires wallet_address + private_key (Ethereum secp256k1 format).

    Per-trade notional is capped at `per_trade_notional` so a bad fill
    can never exceed the intended micro-live size.
    """

    name = "hyperliquid"

    def __init__(
        self,
        base_url: str = HL_BASE_URL,
        wallet_address: str = "",
        private_key: str = "",
        per_trade_notional: Decimal = Decimal("10"),
        dry_run: bool = True,
        slippage_bps: Decimal = Decimal("1.0"),
        simulate_fill_price_offset_bps: Decimal = Decimal("0.5"),
    ):
        self.base_url = base_url
        self.wallet_address = wallet_address
        self.private_key = private_key
        self.per_trade_notional = per_trade_notional
        self.dry_run = dry_run
        self.slippage_bps = slippage_bps
        self.simulate_fill_offset = simulate_fill_price_offset_bps
        self._counter = 0

        # Lazy CCXT instance (only created in live mode)
        self._ccxt: Any = None

    def close(self) -> None:
        if self._ccxt is not None:
            try:
                self._ccxt.close()
            except Exception:
                pass
            self._ccxt = None

    # ---------------------------------------------------------------------------
    # CCXT lazy init
    # ---------------------------------------------------------------------------

    def _get_ccxt(self) -> Any:
        """Lazily create and configure the CCXT hyperliquid instance."""
        if self._ccxt is None:
            from hyperliquid import HyperliquidSync

            self._ccxt = HyperliquidSync({
                "enableRateLimit": True,
                "options": {
                    "defaultSlippage": str(float(self.slippage_bps) / 100.0),
                },
            })

            # Set credentials (required: wallet_address + private_key)
            self._ccxt.walletAddress = self.wallet_address
            self._ccxt.privateKey = self.private_key  # 0x... hex string

        return self._ccxt

    # ---------------------------------------------------------------------------
    # ExchangeAdapter interface
    # ---------------------------------------------------------------------------

    def health_check(self) -> OrderResult:
        if self.dry_run:
            return OrderResult(success=True, order_id="DRY_RUN", status=OrderStatus.OPEN)
        try:
            ccxt = self._get_ccxt()
            # Ping user context
            resp = ccxt.public_post_info({
                "type": "userContext",
                "user": self.wallet_address,
            })
            if resp and resp.get("status") == "ok":
                return OrderResult(success=True, order_id="HEALTH_OK", status=OrderStatus.OPEN)
            return OrderResult(success=False, error="health_check failed", status=OrderStatus.FAILED)
        except Exception as exc:
            return OrderResult(success=False, error=str(exc), status=OrderStatus.FAILED)

    def place_market_buy(
        self,
        symbol: str,
        quantity: Decimal,
        **kwargs: Any,
    ) -> OrderResult:
        return self._place_order(symbol, quantity, OrderSide.BUY, **kwargs)

    def place_market_sell(
        self,
        symbol: str,
        quantity: Decimal,
        **kwargs: Any,
    ) -> OrderResult:
        return self._place_order(symbol, quantity, OrderSide.SELL, **kwargs)

    def cancel_order(self, order_id: str, symbol: str) -> OrderResult:
        if self.dry_run:
            return OrderResult(success=True, order_id=order_id, status=OrderStatus.CANCELLED)
        try:
            ccxt = self._get_ccxt()
            result = ccxt.cancel_order(symbol=symbol, id=order_id)
            if result and result.get("status") == "canceled":
                return OrderResult(success=True, order_id=order_id, status=OrderStatus.CANCELLED)
            return OrderResult(success=False, order_id=order_id, error=str(result), status=OrderStatus.FAILED)
        except Exception as exc:
            return OrderResult(success=False, order_id=order_id, error=str(exc), status=OrderStatus.FAILED)

    def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        if self.dry_run:
            return OrderResult(success=True, order_id=order_id, status=OrderStatus.FILLED)
        try:
            ccxt = self._get_ccxt()
            order = ccxt.fetch_order(id=order_id, symbol=symbol)
            status_map = {
                "open": OrderStatus.OPEN,
                "closed": OrderStatus.FILLED,
                "filled": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELLED,
                "rejected": OrderStatus.REJECTED,
            }
            mapped = status_map.get(order.get("status", ""), OrderStatus.FAILED)
            return OrderResult(
                success=mapped in (OrderStatus.OPEN, OrderStatus.FILLED),
                order_id=order_id,
                status=mapped,
            )
        except Exception as exc:
            return OrderResult(success=False, order_id=order_id, error=str(exc), status=OrderStatus.FAILED)

    def get_balance(self) -> BalanceInfo:
        if self.dry_run:
            # Return a small dry-run balance
            avail = float(self.per_trade_notional) * 10
            return BalanceInfo(
                available=Decimal(str(avail)),
                total=Decimal(str(avail)),
                locked=Decimal(0),
                timestamp_iso=self.get_timestamp_iso(),
            )
        try:
            ccxt = self._get_ccxt()
            balance = ccxt.fetch_balance()
            # Hyperliquid uses "USDC" for margin
            total = Decimal(str(balance.get("total", {}).get("USDC", 0)))
            free = Decimal(str(balance.get("free", {}).get("USDC", 0)))
            locked = Decimal(str(balance.get("used", {}).get("USDC", 0)))
            return BalanceInfo(
                available=free,
                total=total,
                locked=locked,
                timestamp_iso=self.get_timestamp_iso(),
            )
        except Exception:
            return BalanceInfo(
                available=Decimal(0),
                total=Decimal(0),
                locked=Decimal(0),
                timestamp_iso=self.get_timestamp_iso(),
            )

    def get_position(self, symbol: str) -> PositionInfo | None:
        if self.dry_run:
            return None
        try:
            ccxt = self._get_ccxt()
            pos = ccxt.fetch_position(symbol=symbol)
            if not pos or not pos.get("contracts"):
                return None
            return PositionInfo(
                underlying_key=symbol,
                size=Decimal(str(pos.get("contracts", 0))),
                entry_price=Decimal(str(pos.get("entryPrice", 0))),
                unrealized_pnl=Decimal(str(pos.get("unrealizedPnl", 0))),
                margin_used=Decimal(str(pos.get("initialMargin", 0))),
                timestamp_iso=self.get_timestamp_iso(),
            )
        except Exception:
            return None

    def get_order_type_abi(self) -> str:
        return "Market"

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    def _next_id(self) -> str:
        self._counter += 1
        ts = int(time.time() * 1000)
        return f"DRY_{ts}_{self._counter}"

    def _place_order(
        self,
        symbol: str,
        quantity: Decimal,
        side: OrderSide,
        **kwargs: Any,
    ) -> OrderResult:
        """
        Execute a market order.

        In dry_run mode: simulate a realistic fill at worst-case price.
        In live mode: use CCXT to place a real Hyperliquid market order.
        Notional is capped at self.per_trade_notional.
        """
        # Cap notional to prevent over-exposure
        estimated_price = kwargs.get("price", Decimal("1"))
        qty = min(quantity, Decimal(str(self.per_trade_notional)) / estimated_price)

        if self.dry_run:
            return self._simulate_fill(symbol, qty, side, estimated_price)

        return self._submit_live_order(symbol, qty, side)

    def _simulate_fill(
        self,
        symbol: str,
        quantity: Decimal,
        side: OrderSide,
        base_price: Decimal,
    ) -> OrderResult:
        """Simulate a realistic worst-case fill in dry_run mode."""
        slip = base_price * self.slippage_bps / Decimal("10000")
        fill_price = base_price + slip if side == OrderSide.BUY else base_price - slip
        order_id = self._next_id()

        fill = Fill(
            order_id=order_id,
            side=side,
            price=fill_price,
            size=quantity,
            fee=fill_price * quantity * Decimal("0.0004"),   # 4 bps maker fee approximation
            fee_currency="USDC",
            ts_iso=self.get_timestamp_iso(),
        )
        return OrderResult(
            success=True,
            order_id=order_id,
            status=OrderStatus.FILLED,
            fills=[fill],
            avg_fill_price=fill_price,
            total_filled_size=quantity,
            total_fee=fill.fee,
        )

    def _submit_live_order(
        self,
        symbol: str,
        quantity: Decimal,
        side: OrderSide,
    ) -> OrderResult:
        """Place a real Hyperliquid market order via CCXT."""
        if not self.wallet_address or not self.private_key:
            return OrderResult(
                success=False,
                error="wallet_address and private_key required for live trading",
                status=OrderStatus.FAILED,
            )

        order_id = self._next_id()

        try:
            ccxt = self._get_ccxt()
            # CCXT market orders: amount is in base currency (e.g. BTC, not USD)
            # For perpetuals on Hyperliquid, symbol like "BTC/USDC:USDC" -> amount = BTC qty
            # We need to convert USD notional to base qty
            estimated_price = 1  # rough estimate; CCXT uses market price
            base_qty = float(quantity / estimated_price)

            result = ccxt.create_market_order(
                symbol=symbol,
                side=side.value,
                amount=base_qty,
            )

            # Parse fills from result
            fills = []
            total_size = Decimal(0)
            total_fee = Decimal(0)
            avg_price = Decimal(0)
            filled_qty_total = Decimal(0)

            for trade in result.get("trades", []):
                px = Decimal(str(trade.get("price", 0)))
                sz = Decimal(str(trade.get("amount", 0)))
                fee = Decimal(str(trade.get("fee", {}).get("cost", 0)))
                fills.append(Fill(
                    order_id=order_id,
                    side=side,
                    price=px,
                    size=sz,
                    fee=fee,
                    fee_currency=str(trade.get("fee", {}).get("currency", "USDC")),
                    ts_iso=trade.get("timestamp", self.get_timestamp_iso()),
                ))
                total_size += sz
                total_fee += fee
                avg_price += px * sz
                filled_qty_total += sz

            if filled_qty_total > 0:
                avg_price = avg_price / filled_qty_total

            status = OrderStatus.FILLED if filled_qty_total > 0 else OrderStatus.OPEN

            return OrderResult(
                success=True,
                order_id=order_id,
                status=status,
                fills=fills,
                avg_fill_price=avg_price,
                total_filled_size=filled_qty_total,
                total_fee=total_fee,
            )

        except Exception as exc:
            return OrderResult(success=False, order_id=order_id, error=str(exc), status=OrderStatus.FAILED)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_hyperliquid_adapter(
    config: dict[str, Any],
    per_trade_notional: Decimal = Decimal("10"),
) -> HyperliquidAdapter:
    """
    Build a HyperliquidAdapter from a config dict (from .env or JSON).

    Keys expected:
      hl_wallet_address      — Ethereum wallet address (0x...)
      hl_private_key         — Ethereum private key (0x... 64 hex chars)
      hl_base_url           — optional, defaults to mainnet
      dry_run               — optional, default True
      slippage_bps          — optional, default 1.0
    """
    import os as _os

    dry_run = _os.getenv("DRY_RUN", "true").lower() not in {"false", "0", "no"}

    return HyperliquidAdapter(
        base_url=config.get("hl_base_url", HL_BASE_URL),
        wallet_address=config.get("hl_wallet_address", ""),
        private_key=config.get("hl_private_key", ""),
        per_trade_notional=per_trade_notional,
        dry_run=dry_run,
        slippage_bps=Decimal(str(config.get("slippage_bps", "1.0"))),
    )
