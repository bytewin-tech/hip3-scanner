"""Exchange adapter layer — abstracts order execution so live_trader is venue-agnostic."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(Enum):
    PENDING = "pending"       # submitted, not yet acknowledged
    OPEN = "open"             # resting on book
    FILLED = "filled"         # fully filled
    PARTIAL = "partial"       # partially filled
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"         # local error (network, signature, etc.)


@dataclass
class Fill:
    """A single fill event from an order."""
    order_id: str
    side: OrderSide
    price: Decimal          # execution price (in quote currency, e.g. USD)
    size: Decimal           # filled size (in base currency, e.g. BTC)
    fee: Decimal             # fee charged by exchange
    fee_currency: str        # e.g. "USDC"
    ts_iso: str              # fill timestamp ISO string


@dataclass
class OrderResult:
    """Result of placing / checking an order."""
    success: bool
    order_id: str | None = None
    status: OrderStatus = OrderStatus.FAILED
    fills: list[Fill] = field(default_factory=list)
    error: str | None = None
    avg_fill_price: Decimal | None = None
    total_filled_size: Decimal | None = None
    total_fee: Decimal | None = None

    @property
    def filled_qty(self) -> Decimal:
        return sum(f.size for f in self.fills)

    @property
    def is_terminal(self) -> bool:
        """True once the order is in a final state (no more updates expected)."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        )


@dataclass
class PositionInfo:
    """Current position on an exchange."""
    underlying_key: str          # e.g. "BTC"
    size: Decimal                 # positive = long, negative = short
    entry_price: Decimal | None  # average entry price
    unrealized_pnl: Decimal       # in quote currency
    margin_used: Decimal
    timestamp_iso: str


@dataclass
class BalanceInfo:
    """Account balance on an exchange."""
    available: Decimal           # available quote currency (e.g. USDC)
    total: Decimal              # total quote currency
    locked: Decimal = Decimal(0) # locked in open orders
    timestamp_iso: str = ""


class ExchangeAdapter(ABC):
    """Abstract interface for exchange execution. Implement per venue."""

    name: str  # e.g. "hyperliquid", "bybit"

    @abstractmethod
    def health_check(self) -> OrderResult:
        """Ping the exchange to verify connectivity and auth."""
        ...

    @abstractmethod
    def place_market_buy(
        self,
        symbol: str,
        quantity: Decimal,
        # Venue-specific params
        **kwargs: Any,
    ) -> OrderResult:
        ...

    @abstractmethod
    def place_market_sell(
        self,
        symbol: str,
        quantity: Decimal,
        **kwargs: Any,
    ) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> OrderResult:
        ...

    @abstractmethod
    def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        ...

    @abstractmethod
    def get_balance(self) -> BalanceInfo:
        ...

    @abstractmethod
    def get_position(self, symbol: str) -> PositionInfo | None:
        ...

    @abstractmethod
    def get_order_type_abi(self) -> str:
        """Return the ABI type name for order signature (venue-specific)."""
        ...

    # ---- Utility ----

    def get_timestamp_iso(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()
