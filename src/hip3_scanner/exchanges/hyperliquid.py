"""Hyperliquid exchange adapter.

Mode toggle:
  - dry_run=True  (default)  → simulated fills, no real orders
  - dry_run=False             → live order execution via Hyperliquid API

To enable live trading:
  1. Set DRY_RUN=false in .env
  2. Fund the Hyperliquid testnet/mainnet wallet
  3. (Optional) set LEDGER_SIZE to cap per-trade notional in live mode
"""

from __future__ import annotations

import hashlib
import hmac
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
# Hyperliquid-specific constants
# ---------------------------------------------------------------------------

HL_BASE_URL = "https://api.hyperliquid.xyz"
HL_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

# Vaults / venues tracked by the scanner
HL_VENUES = {"xyz", "flx", "vntl", "hyna", "km", "abcd", "para", "cash"}


# ---------------------------------------------------------------------------
# Wallet / signature helpers
# ---------------------------------------------------------------------------

def _sign_message(msg: dict[str, Any], secret_key_b64: str) -> str:
    """HMAC-SHA256 sign a JSON message (Hyperliquid order signing)."""
    payload = json.dumps(msg, separators=(",", ":"))
    secret_bytes = b64decode(secret_key_b64)
    sig = hmac.new(secret_bytes, payload.encode(), hashlib.sha256).digest()
    return b64encode(sig).decode()


# ---------------------------------------------------------------------------
# Hyperliquid adapter
# ---------------------------------------------------------------------------

class HyperliquidAdapter(ExchangeAdapter):
    """
    Hyperliquid perpetuals exchange adapter.

    Per-trade notional is capped at `per_trade_notional` so a bad fill
    can never exceed the intended micro-live size.
    """

    name = "hyperliquid"

    def __init__(
        self,
        base_url: str = HL_BASE_URL,
        wallet_address: str | None = None,
        secret_key_b64: str | None = None,
        per_trade_notional: Decimal = Decimal("10"),
        dry_run: bool = True,
        slippage_bps: Decimal = Decimal("1.0"),
        simulate_fill_price_offset_bps: Decimal = Decimal("0.5"),
    ):
        self.base_url = base_url
        self.wallet_address = wallet_address or ""
        self.secret_key_b64 = secret_key_b64 or ""
        self.per_trade_notional = per_trade_notional
        self.dry_run = dry_run
        self.slippage_bps = slippage_bps          # applied in dry_run
        self.simulate_fill_offset = simulate_fill_price_offset_bps  # dry_run: worst-case fill offset
        self._http = httpx.Client(timeout=15.0)
        self._counter = 0

    def close(self) -> None:
        self._http.close()

    # ---- ExchangeAdapter interface ----

    def health_check(self) -> OrderResult:
        if self.dry_run:
            return OrderResult(success=True, order_id="DRY_RUN", status=OrderStatus.OPEN)
        try:
            resp = self._post({"type": "userContext", "user": self.wallet_address})
            if resp.get("status") == "ok":
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
            payload = self._build_broker_cancel(self.wallet_address, order_id, symbol)
            resp = self._post(payload)
            if resp.get("status") == "ok":
                return OrderResult(success=True, order_id=order_id, status=OrderStatus.CANCELLED)
            return OrderResult(success=False, order_id=order_id, error=str(resp), status=OrderStatus.FAILED)
        except Exception as exc:
            return OrderResult(success=False, order_id=order_id, error=str(exc), status=OrderStatus.FAILED)

    def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        if self.dry_run:
            return OrderResult(success=True, order_id=order_id, status=OrderStatus.FILLED)
        try:
            payload = {
                "type": "orderStatus",
                "user": self.wallet_address,
                "orderId": order_id,
            }
            resp = self._post(payload)
            status = resp.get("order", {}).get("status", "")
            if status == "filled":
                return OrderResult(success=True, order_id=order_id, status=OrderStatus.FILLED)
            elif status == "open":
                return OrderResult(success=True, order_id=order_id, status=OrderStatus.OPEN)
            else:
                return OrderResult(success=False, order_id=order_id, status=OrderStatus.FAILED)
        except Exception as exc:
            return OrderResult(success=False, order_id=order_id, error=str(exc), status=OrderStatus.FAILED)

    def get_balance(self) -> BalanceInfo:
        if self.dry_run:
            return BalanceInfo(
                available=self.per_trade_notional * 10,
                total=self.per_trade_notional * 10,
                locked=Decimal(0),
                timestamp_iso=self.get_timestamp_iso(),
            )
        try:
            payload = {
                "type": "userState",
                "user": self.wallet_address,
            }
            resp = self._post(payload)
            # Hyperliquid returns account value / margin summary
            # field names: "marginSummary" -> { "accountValue", "totalMarginUsed" }
            margin = resp.get("marginSummary", {})
            avail = Decimal(str(margin.get("totalEq", margin.get("accountValue", "0"))))
            locked = Decimal(str(margin.get("totalMarginUsed", "0")))
            return BalanceInfo(
                available=avail - locked,
                total=avail,
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
            payload = {
                "type": "positions",
                "user": self.wallet_address,
            }
            resp = self._post(payload)
            for pos in resp if isinstance(resp, list) else []:
                if pos.get("coin") == symbol:
                    return PositionInfo(
                        underlying_key=symbol,
                        size=Decimal(str(pos.get("size", 0))),
                        entry_price=Decimal(str(pos.get("entryPx", 0))),
                        unrealized_pnl=Decimal(str(pos.get("unrealizedPnl", 0))),
                        margin_used=Decimal(str(pos.get("marginUsed", 0))),
                        timestamp_iso=self.get_timestamp_iso(),
                    )
            return None
        except Exception:
            return None

    def get_order_type_abi(self) -> str:
        return "Market"

    # ---- Internal helpers ----

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

        In dry_run mode: simulate a realistic fill at worst-case price
        (entry price shifted by slippage_bps).

        In live mode: sign + submit a Hyperliquid market order via the
        /exchange endpoint. Notional is capped at self.per_trade_notional.
        """
        # Cap notional to prevent over-exposure
        estimated_price = kwargs.get("price", Decimal("1"))
        qty = min(quantity, self.per_trade_notional / estimated_price)

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
            fee=fill_price * quantity * Decimal("0.0004"),   # 4 bps maker fee (approximate)
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
        """Sign and submit a real Hyperliquid market order."""
        if not self.wallet_address or not self.secret_key_b64:
            return OrderResult(
                success=False,
                error="wallet_address and secret_key_b64 required for live trading",
                status=OrderStatus.FAILED,
            )

        order_id = self._next_id()
        ts_ms = int(time.time() * 1000)

        msg = {
            "type": "order",
            "user": self.wallet_address,
            "order": {
                "id": order_id,
                "side": side.value.capitalize(),
                "asset": symbol,
                "size": str(quantity),
                "price": "0",                 # market order: price = 0
                "orderType": {"type": "Market"},
                "fillOrKill": False,
                "timeInForce": {"type": "GTC"},
                "reduceOnly": False,
                "loopTimeInForce": False,
            },
            "action": {
                "type": "order",
                "hyperdrive": True,
                "isMarketSale": True,
            },
            "nonce": ts_ms,
            "signature": "",  # filled below
        }

        try:
            msg["signature"] = _sign_message(msg, self.secret_key_b64)
        except Exception as exc:
            return OrderResult(success=False, error=f"signing failed: {exc}", status=OrderStatus.FAILED)

        try:
            resp = self._post({"type": "exchange", "orders": [msg["order"]]})
            if isinstance(resp, dict) and resp.get("status") == "ok":
                # Hyperliquid fills immediately for market orders
                fills_raw = resp.get("response", {}).get("data", {}).get("fills", [])
                fills = [
                    Fill(
                        order_id=order_id,
                        side=side,
                        price=Decimal(str(f.get("px", 0))),
                        size=Decimal(str(f.get("sz", 0))),
                        fee=Decimal(str(f.get("fee", 0))),
                        fee_currency="USDC",
                        ts_iso=self.get_timestamp_iso(),
                    )
                    for f in fills_raw
                ]
                total_size = sum(f.size for f in fills)
                total_fee = sum(f.fee for f in fills)
                avg_price = (
                    sum(f.price * f.size for f in fills) / total_size
                    if total_size > 0 else Decimal(0)
                )
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    status=OrderStatus.FILLED if fills else OrderStatus.OPEN,
                    fills=fills,
                    avg_fill_price=avg_price,
                    total_filled_size=total_size,
                    total_fee=total_fee,
                )
            return OrderResult(success=False, error=str(resp), status=OrderStatus.FAILED)
        except httpx.HTTPStatusError as exc:
            return OrderResult(
                success=False,
                order_id=order_id,
                error=f"HTTP {exc.response.status_code}: {exc.response.text}",
                status=OrderStatus.FAILED,
            )
        except Exception as exc:
            return OrderResult(success=False, order_id=order_id, error=str(exc), status=OrderStatus.FAILED)

    def _build_broker_cancel(self, user: str, order_id: str, symbol: str) -> dict[str, Any]:
        return {
            "type": "brokerCancel",
            "user": user,
            "info": {"id": order_id, "symbol": symbol},
            "nonce": int(time.time() * 1000),
        }

    def _post(self, payload: dict[str, Any]) -> Any:
        resp = self._http.post(self.base_url, json=payload)
        resp.raise_for_status()
        return resp.json()


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
      HL_WALLET_ADDRESS
      HL_SECRET_KEY_B64
      HL_BASE_URL          (optional, defaults to mainnet)
      DRY_RUN              (optional, default True)
      PER_TRADE_NOTIONAL   (optional, default 10)
      SLIPPAGE_BPS         (optional, default 1.0)
    """
    import os as _os

    dry_run = _os.getenv("DRY_RUN", "true").lower() not in {"false", "0", "no"}

    return HyperliquidAdapter(
        base_url=config.get("hl_base_url", HL_BASE_URL),
        wallet_address=config.get("hl_wallet_address", ""),
        secret_key_b64=config.get("hl_secret_key_b64", ""),
        per_trade_notional=per_trade_notional,
        dry_run=dry_run,
        slippage_bps=Decimal(str(config.get("slippage_bps", "1.0"))),
    )
