from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


ZERO = Decimal("0")


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(slots=True)
class VenueInfo:
    name: str
    full_name: str | None
    asset_to_streaming_oi_cap: dict[str, Any]
    asset_to_funding_multiplier: dict[str, Any]
    asset_to_funding_interest_rate: dict[str, Any]


@dataclass(slots=True)
class MarketSnapshot:
    ts_ms: int
    venue: str
    venue_full_name: str | None
    market_id: str
    underlying_key: str
    category: str | None
    sz_decimals: int | None
    max_leverage: int | None
    margin_table_id: int | None
    only_isolated: bool
    margin_mode: str | None
    is_delisted: bool
    growth_mode: str | None
    last_growth_mode_change_time: Any
    mark_px: Decimal | None
    mid_px: Decimal | None
    oracle_px: Decimal | None
    impact_bid_px: Decimal | None
    impact_ask_px: Decimal | None
    funding: Decimal | None
    premium: Decimal | None
    open_interest: Decimal | None
    day_ntl_vlm: Decimal | None
    day_base_vlm: Decimal | None
    prev_day_px: Decimal | None
    streaming_oi_cap: Decimal | None
    funding_multiplier: Decimal | None
    interest_rate_override: Decimal | None

    @property
    def has_impact(self) -> bool:
        return self.impact_bid_px is not None and self.impact_ask_px is not None

    @property
    def is_liquid_enough(self) -> bool:
        return (self.day_ntl_vlm or ZERO) > ZERO and (self.open_interest or ZERO) > ZERO
